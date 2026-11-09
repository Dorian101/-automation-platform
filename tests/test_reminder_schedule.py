import logging
import re
from datetime import UTC, datetime, time, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.config import Config
from app.core.identity import TELEGRAM, WEB, Identity
from app.core.results import CommandError
from app.db.held_repo import HeldNotificationsRepository
from app.db.links_repo import TelegramLinksRepository
from app.notifications import Notifier, QuietHours, within
from app.plugins.manager import PluginManager
from app.plugins.reminders import (
    RemindersPlugin,
    next_occurrence,
    snooze_until,
)
from tests.conftest import TEST_USERNAME

OWNER = Identity(kind=WEB, id=TEST_USERNAME)


@pytest.fixture(autouse=True)
def moscow(monkeypatch):
    monkeypatch.setattr(Config, "DISPLAY_TIMEZONE", "Europe/Moscow")


class Recording:
    def __init__(self):
        self.calls = []

    async def deliver(self, target, text, **kwargs):
        self.calls.append((target, text, kwargs))
        return True

    def reachable(self, target):
        return True


class Channel:
    kind = TELEGRAM

    def __init__(self):
        self.calls = []

    async def deliver(self, target, text, url="/", buttons=()):
        self.calls.append((target, text, url, list(buttons)))
        return True


def _past(minutes=1):
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes)


class TestNextOccurrence:
    def test_daily_keeps_the_wall_clock_time(self):
        friday_nine_msk = datetime(2026, 11, 13, 6, 0)

        assert next_occurrence(
            friday_nine_msk, "daily", datetime(2026, 11, 13, 6, 1, tzinfo=UTC)
        ) == datetime(2026, 11, 14, 6, 0)

    def test_weekdays_skip_the_weekend(self):
        friday_nine_msk = datetime(2026, 11, 13, 6, 0)

        assert next_occurrence(
            friday_nine_msk, "weekdays", datetime(2026, 11, 13, 6, 1, tzinfo=UTC)
        ) == datetime(2026, 11, 16, 6, 0)

    def test_weekly_is_seven_days_on(self):
        assert next_occurrence(
            datetime(2026, 11, 13, 6, 0), "weekly", datetime(2026, 11, 13, tzinfo=UTC)
        ) == datetime(2026, 11, 20, 6, 0)

    def test_a_missed_series_is_never_scheduled_in_the_past(self):
        now = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)

        assert next_occurrence(datetime(2026, 11, 1, 6, 0), "daily", now) == datetime(
            2026, 12, 1, 6, 0
        )


class TestSnoozeUntil:
    def test_minutes(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

        assert snooze_until("10", now) == datetime(2026, 11, 9, 12, 10)
        assert snooze_until("60", now) == datetime(2026, 11, 9, 13, 0)

    def test_tomorrow_is_nine_in_the_morning_local(self):
        late_evening_msk = datetime(2026, 11, 9, 20, 30, tzinfo=UTC)

        assert snooze_until("tm", late_evening_msk) == datetime(2026, 11, 10, 6, 0)

    def test_tomorrow_after_midnight_is_this_morning(self):
        half_past_midnight_msk = datetime(2026, 11, 9, 21, 30, tzinfo=UTC)

        assert snooze_until("tm", half_past_midnight_msk) == datetime(
            2026, 11, 10, 6, 0
        )


class TestRepeatingDelivery:
    async def test_a_repeating_reminder_schedules_the_next_one(
        self, database_with_schema
    ):
        notifier = Recording()
        plugin = RemindersPlugin(notifier, database_with_schema)
        plugin.repo.add(OWNER, "water plants", _past(), "daily")

        await plugin.process_due()

        pending = plugin.repo.entries(OWNER)
        assert len(pending) == 1
        assert pending[0][1] == "water plants"
        assert pending[0][2] > datetime.now(UTC).replace(tzinfo=None)

    async def test_delivery_links_the_page_and_offers_snooze(
        self, database_with_schema
    ):
        notifier = Recording()
        plugin = RemindersPlugin(notifier, database_with_schema)
        reminder_id = plugin.repo.add(OWNER, "call", _past())

        await plugin.process_due()

        _, _, kwargs = notifier.calls[0]
        assert kwargs["url"] == "/reminders"
        assert (
            "10 минут", f"rs:{reminder_id}:10"
        ) in kwargs["buttons"]

    async def test_a_one_off_reminder_is_not_repeated(self, database_with_schema):
        plugin = RemindersPlugin(Recording(), database_with_schema)
        plugin.repo.add(OWNER, "once", _past())

        await plugin.process_due()

        assert plugin.repo.entries(OWNER) == []


class TestSnooze:
    async def test_snoozing_a_fired_reminder_brings_it_back_once(
        self, database_with_schema
    ):
        plugin = RemindersPlugin(Recording(), database_with_schema)
        reminder_id = plugin.repo.add(OWNER, "stretch", _past(), "daily")
        await plugin.process_due()

        await plugin.page_action(
            OWNER, "snooze", {"id": str(reminder_id), "until": "10"}
        )

        pending = plugin.repo.page_entries(OWNER, _past(60))
        assert len([one for one in pending if not one["sent"]]) == 2
        snoozed = next(one for one in pending if one["id"] == reminder_id)
        assert snoozed["repeat"] is None

    async def test_the_bot_snoozes_only_its_own_reminders(self, database_with_schema):
        plugin = RemindersPlugin(Recording(), database_with_schema)
        mine = plugin.repo.add(Identity(TELEGRAM, "42"), "mine", _past())
        theirs = plugin.repo.add(Identity(TELEGRAM, "7"), "theirs", _past())
        await plugin.process_due()

        assert plugin.snooze_from_chat(42, f"rs:{mine}:60").startswith("Отложено")
        assert plugin.snooze_from_chat(42, f"rs:{theirs}:60") == (
            "Напоминание не найдено."
        )
        assert plugin.snooze_from_chat(42, "rs:x:60") == "Некорректная кнопка."

    async def test_an_unknown_snooze_is_refused(self, database_with_schema):
        plugin = RemindersPlugin(Recording(), database_with_schema)

        with pytest.raises(CommandError):
            await plugin.page_action(OWNER, "snooze", {"id": "1", "until": "999"})


class TestThePage:
    async def test_adding_from_the_page(self, database_with_schema):
        plugin = RemindersPlugin(Recording(), database_with_schema)
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT09:00")

        view = await plugin.page_action(
            OWNER, "add", {"text": "dentist", "at": tomorrow, "repeat": "weekdays"}
        )

        upcoming = view["list"][-1]["rows"]
        assert upcoming[0]["text"] == "dentist"
        assert upcoming[0]["repeat"] == "По будням"

    @pytest.mark.parametrize(
        "payload",
        [
            {"text": "", "at": "2030-01-01T09:00"},
            {"text": "x", "at": "nonsense"},
            {"text": "x", "at": "2001-01-01T09:00"},
            {"text": "x", "at": "2030-01-01T09:00", "repeat": "hourly"},
        ],
    )
    async def test_bad_input_is_refused(self, database_with_schema, payload):
        plugin = RemindersPlugin(Recording(), database_with_schema)

        with pytest.raises(CommandError):
            await plugin.page_action(OWNER, "add", payload)


class TestWithin:
    def test_a_window_across_midnight(self):
        window = (time(23, 0), time(8, 0))

        assert within(window, time(23, 30))
        assert within(window, time(7, 59))
        assert not within(window, time(8, 0))
        assert not within(window, time(12, 0))

    def test_a_window_inside_one_day(self):
        assert within((time(13, 0), time(14, 0)), time(13, 30))
        assert not within((time(13, 0), time(14, 0)), time(14, 30))


class TestQuietHours:
    @pytest.fixture
    def owner(self, users, create_user):
        return users.get_by_username(TEST_USERNAME)

    def _notifier(self, database, users, channel, hour):
        clock = {"now": datetime(2026, 11, 9, hour, 0, tzinfo=UTC)}
        quiet = QuietHours(
            users,
            TelegramLinksRepository(database),
            HeldNotificationsRepository(database),
            clock=lambda: clock["now"],
        )
        return Notifier([channel], quiet=quiet), clock

    async def test_a_night_message_waits_for_the_morning(
        self, database_with_schema, users, owner
    ):
        users.set_quiet_hours(owner.id, time(23, 0), time(8, 0))
        target = Identity(TELEGRAM, "42")
        TelegramLinksRepository(database_with_schema).link(owner.id, 42)
        channel = Channel()
        notifier, clock = self._notifier(database_with_schema, users, channel, 22)

        assert await notifier.deliver(target, "night", url="/reminders",
                                      buttons=[("10 минут", "rs:1:10")])
        assert channel.calls == []

        await notifier.release_held()
        assert channel.calls == []

        clock["now"] = datetime(2026, 11, 10, 5, 0, tzinfo=UTC)
        await notifier.release_held()

        assert channel.calls == [
            (target, "night", "/reminders", [("10 минут", "rs:1:10")])
        ]
        await notifier.release_held()
        assert len(channel.calls) == 1

    async def test_a_message_that_keeps_failing_is_dropped_after_a_day(
        self, database_with_schema, users, owner
    ):
        users.set_quiet_hours(owner.id, time(23, 0), time(8, 0))
        TelegramLinksRepository(database_with_schema).link(owner.id, 42)

        class Broken(Channel):
            async def deliver(self, *args, **kwargs):
                raise RuntimeError("blocked")

        held = HeldNotificationsRepository(database_with_schema)
        notifier, clock = self._notifier(database_with_schema, users, Broken(), 22)
        await notifier.deliver(Identity(TELEGRAM, "42"), "night")

        clock["now"] = datetime(2026, 11, 10, 5, 0, tzinfo=UTC)
        await notifier.release_held()
        assert len(held.entries()) == 1

        clock["now"] = (datetime.now(UTC) + timedelta(days=2)).replace(hour=12)
        await notifier.release_held()
        assert held.entries() == []

    async def test_urgent_and_daytime_messages_go_at_once(
        self, database_with_schema, users, owner
    ):
        users.set_quiet_hours(owner.id, time(23, 0), time(8, 0))
        TelegramLinksRepository(database_with_schema).link(owner.id, 42)
        channel = Channel()
        notifier, clock = self._notifier(database_with_schema, users, channel, 22)

        await notifier.deliver(Identity(TELEGRAM, "42"), "test", urgent=True)
        clock["now"] = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
        await notifier.deliver(Identity(TELEGRAM, "42"), "noon")

        assert [call[1] for call in channel.calls] == ["test", "noon"]

    async def test_without_quiet_hours_nothing_is_held(
        self, database_with_schema, users, owner
    ):
        TelegramLinksRepository(database_with_schema).link(owner.id, 42)
        channel = Channel()
        notifier, _ = self._notifier(database_with_schema, users, channel, 22)

        await notifier.deliver(Identity(TELEGRAM, "42"), "night")

        assert len(channel.calls) == 1


class TestQuietHoursForm:
    @pytest.fixture
    async def client(
        self, web_server_factory, database_with_schema, create_user, login
    ):
        server = web_server_factory(
            PluginManager(logging.getLogger("test")), database_with_schema
        )
        async with TestClient(TestServer(server.build_app())) as test_client:
            await login(test_client)
            yield test_client

    async def _csrf(self, client):
        body = await (await client.get("/")).text()
        return re.search(r'action="/account/quiet".*?name="csrf" value="([^"]+)"',
                         body, re.S).group(1)

    async def test_set_and_clear(self, client, users):
        token = await self._csrf(client)

        await client.post(
            "/account/quiet", data={"csrf": token, "from": "22:30", "to": "07:15"}
        )
        owner = users.get_by_username(TEST_USERNAME)
        assert users.quiet_hours(owner.id) == (time(22, 30), time(7, 15))
        assert "с 22:30 до 07:15" in await (await client.get("/")).text()

        await client.post("/account/quiet", data={"csrf": token, "off": "1",
                                                  "from": "22:30", "to": "07:15"})
        assert users.quiet_hours(owner.id) is None

    async def test_a_forged_form_is_refused(self, client, users):
        await client.post(
            "/account/quiet", data={"csrf": "nope", "from": "22:00", "to": "07:00"}
        )

        assert users.quiet_hours(users.get_by_username(TEST_USERNAME).id) is None


async def test_the_reminders_page_refreshes_itself(database_with_schema):
    plugin = RemindersPlugin(Recording(), database_with_schema)

    view = await plugin.page_action(OWNER, "view", {})

    assert view["live"] is True
