from datetime import date, timedelta

import pytest

from app.core.identity import WEB, Identity
from app.core.results import CommandError
from app.plugins.applications import SILENCE_DAYS, ApplicationsPlugin

OTHER = Identity(kind=WEB, id="someone-else")


@pytest.fixture
def plugin(database_with_schema):
    return ApplicationsPlugin(database_with_schema)


def _days_ago(days: int) -> date:
    return date.today() - timedelta(days=days)


def _row(view, company):
    for row in view["table"]["rows"]:
        if row["company"] == company:
            return row
    raise AssertionError(f"no row for {company}")


class TestStages:
    """The state machine, which is the part of this worth being strict about.

    A status that could be set to anything would make the funnel a set of
    numbers that do not mean anything: an application quietly returned to the
    first stage would inflate the count it is the denominator of.
    """

    async def test_a_new_application_starts_at_the_first_stage(
        self, plugin, web_identity
    ):
        plugin.repo.add(web_identity, "Ромашка", "BI-аналитик", _days_ago(1))

        view = await plugin.page_view(web_identity)

        assert view["table"]["rows"][0]["status"] == "applied"

    async def test_applied_moves_to_answered_or_rejected(self, plugin, web_identity):
        row = await _add_one(plugin, web_identity)

        assert [m["status"] for m in row["moves"]] == ["answered", "rejected"]

    async def test_answered_moves_to_interview_or_rejected(self, plugin, web_identity):
        entry = await _add_one(plugin, web_identity)
        await plugin.page_action(
            web_identity, "stage", {"id": str(entry["id"]), "status": "answered"}
        )

        row = _row(await plugin.page_view(web_identity), entry["company"])

        assert [m["status"] for m in row["moves"]] == ["interview", "rejected"]

    async def test_a_terminal_stage_offers_nothing(self, plugin, web_identity):
        entry = await _add_one(plugin, web_identity)
        await plugin.page_action(
            web_identity, "stage", {"id": str(entry["id"]), "status": "rejected"}
        )

        row = _row(await plugin.page_view(web_identity), entry["company"])

        assert row["moves"] == []

    async def test_a_move_that_is_not_offered_is_refused(
        self, plugin, web_identity
    ):
        """Skipping a stage has to be refused, not quietly accepted.

        This is the case a free dropdown would allow and the one that would
        make every percentage on the page meaningless.
        """
        entry = await _add_one(plugin, web_identity)

        for target in ("interview", "offer"):
            with pytest.raises(CommandError):
                await plugin.page_action(
                    web_identity,
                    "stage",
                    {"id": str(entry["id"]), "status": target},
                )

        row = _row(await plugin.page_view(web_identity), entry["company"])
        assert row["status"] == "applied"

    async def test_a_stage_cannot_be_set_twice_over_the_same_value(
        self, plugin, web_identity
    ):
        """Setting answered while already answered is not a transition."""
        entry = await _add_one(plugin, web_identity)
        await plugin.page_action(
            web_identity, "stage", {"id": str(entry["id"]), "status": "answered"}
        )

        with pytest.raises(CommandError):
            await plugin.page_action(
                web_identity,
                "stage",
                {"id": str(entry["id"]), "status": "answered"},
            )

    async def test_a_full_run_through_the_pipeline(self, plugin, web_identity):
        entry = await _add_one(plugin, web_identity)
        application_id = str(entry["id"])

        for target in ("answered", "interview", "offer"):
            await plugin.page_action(
                web_identity,
                "stage",
                {"id": application_id, "status": target},
            )

        row = _row(await plugin.page_view(web_identity), entry["company"])

        assert row["status"] == "offer"
        assert row["moves"] == []


async def _add_one(plugin, identity, company="Ромашка", days=0) -> dict:
    await plugin.page_action(
        identity,
        "add",
        {
            "company": company,
            "role": "BI-аналитик",
            "applied_at": _days_ago(days).isoformat(),
        },
    )
    return _row(await plugin.page_view(identity), company)


async def _move(plugin, identity, company, target):
    entry = _row(await plugin.page_view(identity), company)
    await plugin.page_action(
        identity, "stage", {"id": str(entry["id"]), "status": target}
    )


async def _advance_to(plugin, identity, company, target):
    """Walk an application to a stage one legal step at a time.

    The stages are a chain, so a test that wants to reach an interview has to
    go through the answer that came before it — jumping straight there is the
    move the machine refuses, and a test that did it would be asserting
    against the behaviour rather than exercising it.
    """
    steps = {
        "answered": ("answered",),
        "interview": ("answered", "interview"),
        "offer": ("answered", "interview", "offer"),
    }

    for step in steps[target]:
        await _move(plugin, identity, company, step)


async def _funnel_item(view, label):
    return next(item for item in view["funnel"]["items"] if item["label"] == label)


class TestFunnel:
    async def test_empty_starts_with_no_numbers(self, plugin, web_identity):
        view = await plugin.page_view(web_identity)

        assert view["funnel"]["items"][0]["share_text"] == "—"
        assert view["funnel"]["rejected"]["count"] == 0

    async def test_shares_are_of_everything_that_was_sent(
        self, plugin, web_identity
    ):
        for index in range(4):
            await _add_one(plugin, web_identity, company=f"Компания {index}")

        for company in ("Компания 0", "Компания 1"):
            await _move(plugin, web_identity, company, "answered")

        view = await plugin.page_view(web_identity)
        answered = await _funnel_item(view, "Ответили")

        assert answered["count"] == 2
        assert answered["share_text"] == "50%"

    async def test_a_later_stage_counts_as_having_passed_the_earlier_one(
        self, plugin, web_identity
    ):
        """The bug this shape exists to catch.

        A status is the stage an application is in now. An application at
        "interview" has already been answered, so counting only the rows
        currently marked "answered" would drop it from the stage above and
        report a conversion that is too low.
        """
        await _add_one(plugin, web_identity, company="Ромашка")
        await _move(plugin, web_identity, "Ромашка", "answered")
        await _move(plugin, web_identity, "Ромашка", "interview")

        view = await plugin.page_view(web_identity)

        assert (await _funnel_item(view, "Ответили"))["count"] == 1
        assert (await _funnel_item(view, "Дошли до собеседования"))["count"] == 1

    async def test_the_first_stage_is_everything_sent(self, plugin, web_identity):
        for index in range(3):
            await _add_one(plugin, web_identity, company=f"Компания {index}")

        await _advance_to(plugin, web_identity, "Компания 0", "offer")
        await _move(plugin, web_identity, "Компания 1", "rejected")

        view = await plugin.page_view(web_identity)

        assert (await _funnel_item(view, "Откликов отправлено"))["count"] == 3
        assert (await _funnel_item(view, "Получили оффер"))["count"] == 1

    async def test_rejections_stay_in_the_denominator(self, plugin, web_identity):
        """Excluding them would flatter every later stage.

        A rejection is an outcome of having applied, so it belongs in what
        the shares are of — it just does not belong as a rung.
        """
        for index in range(2):
            await _add_one(plugin, web_identity, company=f"Компания {index}")

        await _move(plugin, web_identity, "Компания 0", "rejected")
        await _move(plugin, web_identity, "Компания 1", "answered")

        view = await plugin.page_view(web_identity)

        assert (await _funnel_item(view, "Откликов отправлено"))["share_text"] == (
            "100%"
        )
        assert (await _funnel_item(view, "Ответили"))["share_text"] == "50%"
        assert view["funnel"]["rejected"]["count"] == 1

    async def test_rejections_are_not_drawn_as_a_stage(self, plugin, web_identity):
        await _add_one(plugin, web_identity)
        await _move(plugin, web_identity, "Ромашка", "rejected")

        view = await plugin.page_view(web_identity)

        assert view["funnel"]["rejected"]["count"] == 1
        labels = [item["label"] for item in view["funnel"]["items"]]
        assert "Отказ" not in labels

    async def test_the_funnel_accounts_for_every_row(
        self, plugin, web_identity
    ):
        """Rejections plus whatever is still live adds up to the list."""
        for index in range(3):
            await _add_one(plugin, web_identity, company=f"Компания {index}")

        await _move(plugin, web_identity, "Компания 0", "rejected")
        await _advance_to(plugin, web_identity, "Компания 1", "interview")

        view = await plugin.page_view(web_identity)

        rejected = view["funnel"]["rejected"]["count"]
        sent = (await _funnel_item(view, "Откликов отправлено"))["count"]

        assert sent == len(view["table"]["rows"])
        assert rejected == 1


class TestSilence:
    async def test_recent_application_is_not_flagged(self, plugin, web_identity):
        await _add_one(plugin, web_identity, days=1)

        row = _row(await plugin.page_view(web_identity), "Ромашка")

        assert row["silent"] is False
        assert row["silent_days"] is None

    async def test_one_past_the_threshold_is_flagged(
        self, plugin, web_identity
    ):
        await _add_one(plugin, web_identity, days=SILENCE_DAYS)

        row = _row(await plugin.page_view(web_identity), "Ромашка")

        assert row["silent"] is True
        assert row["silent_days"] == SILENCE_DAYS

    async def test_a_answered_application_has_no_silence_to_report(
        self, plugin, web_identity
    ):
        """Days counted past an answer would keep growing on a row that can
        never change again, and would say nothing about what is waiting."""
        entry = await _add_one(plugin, web_identity, days=30)
        await plugin.page_action(
            web_identity, "stage", {"id": str(entry["id"]), "status": "answered"}
        )

        row = _row(await plugin.page_view(web_identity), entry["company"])

        assert row["silent_days"] is None

    async def test_age_is_still_shown_for_every_row(self, plugin, web_identity):
        """Age is a fact about the application; silence is about waiting."""
        entry = await _add_one(plugin, web_identity, days=30)
        await plugin.page_action(
            web_identity, "stage", {"id": str(entry["id"]), "status": "answered"}
        )

        row = _row(await plugin.page_view(web_identity), entry["company"])

        assert row["age_days"] == 30


class TestActions:
    async def test_company_and_role_are_required(self, plugin, web_identity):
        with pytest.raises(CommandError):
            await plugin.page_action(web_identity, "add", {"role": "BI"})

        with pytest.raises(CommandError):
            await plugin.page_action(web_identity, "add", {"company": "Ромашка"})

    async def test_a_future_date_is_refused(self, plugin, web_identity):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()

        with pytest.raises(CommandError):
            await plugin.page_action(
                web_identity,
                "add",
                {
                    "company": "Ромашка",
                    "role": "BI",
                    "applied_at": tomorrow,
                },
            )

    async def test_a_malformed_date_is_refused(self, plugin, web_identity):
        with pytest.raises(CommandError):
            await plugin.page_action(
                web_identity,
                "add",
                {
                    "company": "Ромашка",
                    "role": "BI",
                    "applied_at": "01.10.2026",
                },
            )

    async def test_another_accounts_application_cannot_be_moved(
        self, plugin, web_identity
    ):
        entry = await _add_one(plugin, web_identity)

        with pytest.raises(CommandError):
            await plugin.page_action(
                OTHER,
                "stage",
                {"id": str(entry["id"]), "status": "answered"},
            )

    async def test_another_account_cannot_delete_it(self, plugin, web_identity):
        entry = await _add_one(plugin, web_identity)

        with pytest.raises(CommandError):
            await plugin.page_action(OTHER, "delete", {"id": str(entry["id"])})

        assert (await plugin.page_view(web_identity))["table"]["rows"] != []

    async def test_unknown_stage_is_refused(self, plugin, web_identity):
        entry = await _add_one(plugin, web_identity)

        with pytest.raises(CommandError):
            await plugin.page_action(
                web_identity,
                "stage",
                {"id": str(entry["id"]), "status": "hired"},
            )


class TestIsolation:
    async def test_one_accounts_applications_are_absent_from_the_other(
        self, plugin, web_identity
    ):
        await _add_one(plugin, web_identity, company="Моя компания")

        view = await plugin.page_view(OTHER)

        assert view["table"]["rows"] == []
        assert view["totals"]["items"][0]["value"] == 0


class TestUrl:
    """A stored link is an href the browser will follow.

    That makes the scheme part of the input rather than a detail: a
    `javascript:` URL clicked from this table would run in the page's own
    context, so it is refused at the point the value enters.
    """

    async def _add(self, plugin, identity, url):
        await plugin.page_action(
            identity,
            "add",
            {
                "company": "Ромашка",
                "role": "BI-аналитик",
                "url": url,
            },
        )

    async def test_a_https_link_is_stored(self, plugin, web_identity):
        await self._add(plugin, web_identity, "https://hh.ru/vacancy/1")

        row = _row(await plugin.page_view(web_identity), "Ромашка")

        assert row["url"] == "https://hh.ru/vacancy/1"

    async def test_http_is_allowed_too(self, plugin, web_identity):
        await self._add(plugin, web_identity, "http://example.com/job")

        row = _row(await plugin.page_view(web_identity), "Ромашка")

        assert row["url"] == "http://example.com/job"

    async def test_no_link_is_fine(self, plugin, web_identity):
        await self._add(plugin, web_identity, "")

        row = _row(await plugin.page_view(web_identity), "Ромашка")

        assert row["url"] == ""

    async def test_a_javascript_url_is_refused(self, plugin, web_identity):
        with pytest.raises(CommandError):
            await self._add(
                plugin, web_identity, "javascript:alert(document.cookie)"
            )

    async def test_a_scheme_is_not_enough_on_its_own(self, plugin, web_identity):
        for bad in (
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "//example.com/job",
            "hh.ru/vacancy/1",
        ):
            with pytest.raises(CommandError):
                await self._add(plugin, web_identity, bad)

    async def test_the_check_does_not_depend_on_case(self, plugin, web_identity):
        await self._add(plugin, web_identity, "HTTPS://hh.ru/vacancy/1")

        row = _row(await plugin.page_view(web_identity), "Ромашка")

        assert row["url"] == "HTTPS://hh.ru/vacancy/1"

    async def test_an_overlong_link_is_refused(self, plugin, web_identity):
        with pytest.raises(CommandError):
            await self._add(plugin, web_identity, "https://x.ru/" + "a" * 500)

    async def test_the_url_column_is_marked_as_a_link(
        self, plugin, web_identity
    ):
        """How the cell is drawn is the column's business, not the value's."""
        view = await plugin.page_view(web_identity)
        columns = {c["key"]: c for c in view["table"]["columns"]}

        assert columns["url"]["link"] is True
        assert columns["company"].get("link") is None
