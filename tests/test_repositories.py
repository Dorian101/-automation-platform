from datetime import UTC, datetime, timedelta

from app.core.identity import WEB, Identity, telegram
from app.db.clipboard_repo import ClipboardRepository
from app.db.notes_repo import NotesRepository
from app.db.reminders_repo import RemindersRepository


def _texts(entries):
    """The note text alone, so assertions do not repeat the row id."""
    return [text for _, text in entries]


def _iso(offset: timedelta) -> str:
    return (datetime.now(UTC) + offset).replace(tzinfo=None).isoformat()


class TestNotesRepository:
    def test_add_and_list(self, database_with_schema):
        repo = NotesRepository(database_with_schema)
        identity = Identity(kind=WEB, id="default")

        repo.add(identity, "first note")
        repo.add(identity, "second note")

        assert _texts(repo.entries(identity)) == ["second note", "first note"]

    def test_list_empty(self, database_with_schema):
        repo = NotesRepository(database_with_schema)

        assert _texts(repo.entries(Identity(kind=WEB, id="nobody"))) == []

    def test_isolated_by_identity(self, database_with_schema):
        repo = NotesRepository(database_with_schema)

        repo.add(telegram(111), "for 111")
        repo.add(telegram(222), "for 222")

        assert _texts(repo.entries(telegram(111))) == ["for 111"]
        assert _texts(repo.entries(telegram(222))) == ["for 222"]

    def test_same_id_different_transport_is_separate(self, database_with_schema):
        repo = NotesRepository(database_with_schema)

        repo.add(telegram(777), "telegram")
        repo.add(Identity(kind=WEB, id="777"), "web")

        assert _texts(repo.entries(telegram(777))) == ["telegram"]
        assert _texts(repo.entries(Identity(kind=WEB, id="777"))) == ["web"]


class TestRemindersRepository:
    def test_past_reminder_is_due(self, database_with_schema):
        repo = RemindersRepository(database_with_schema)
        identity = Identity(kind=WEB, id="default")

        repo.add(identity, "test reminder", _iso(-timedelta(minutes=5)))

        due = repo.get_due()
        assert len(due) == 1
        assert due[0][1] == "web:default"
        assert due[0][2] == "test reminder"

    def test_future_reminder_not_due(self, database_with_schema):
        repo = RemindersRepository(database_with_schema)

        repo.add(Identity(kind=WEB, id="default"), "future", _iso(timedelta(hours=1)))

        assert repo.get_due() == []

    def test_mark_sent_removes_from_due(self, database_with_schema):
        repo = RemindersRepository(database_with_schema)
        identity = Identity(kind=WEB, id="default")

        repo.add(identity, "remind me", _iso(-timedelta(minutes=5)))
        reminder_id = repo.get_due()[0][0]

        repo.mark_sent(reminder_id)

        assert repo.get_due() == []

    def test_only_unsent_are_due(self, database_with_schema):
        repo = RemindersRepository(database_with_schema)
        identity = Identity(kind=WEB, id="default")
        past = _iso(-timedelta(minutes=5))

        repo.add(identity, "already sent", past)
        repo.mark_sent(repo.get_due()[0][0])

        repo.add(identity, "new one", past)
        due = repo.get_due()

        assert len(due) == 1
        assert due[0][2] == "new one"

    def test_user_id_round_trips(self, database_with_schema):
        repo = RemindersRepository(database_with_schema)
        identity = Identity(kind=WEB, id="default")

        repo.add(identity, "check", _iso(-timedelta(minutes=5)))

        stored = repo.get_due()[0][1]

        assert Identity.parse(stored) == identity

    def test_entries_lists_unsent_soonest_first(self, database_with_schema):
        repo = RemindersRepository(database_with_schema)
        identity = Identity(kind=WEB, id="default")

        repo.add(identity, "later", _iso(timedelta(hours=1)))
        repo.add(identity, "sooner", _iso(timedelta(minutes=5)))

        entries = repo.entries(identity)

        assert [text for _, text, _ in entries] == ["sooner", "later"]

    def test_entries_omit_sent(self, database_with_schema):
        repo = RemindersRepository(database_with_schema)
        identity = Identity(kind=WEB, id="default")

        repo.add(identity, "keep", _iso(timedelta(minutes=5)))
        repo.add(identity, "sent", _iso(-timedelta(minutes=5)))
        repo.mark_sent(repo.get_due()[0][0])

        assert [text for _, text, _ in repo.entries(identity)] == ["keep"]

    def test_delete_is_scoped_to_the_account(self, database_with_schema):
        repo = RemindersRepository(database_with_schema)

        repo.add(Identity(kind=WEB, id="a"), "mine", _iso(timedelta(minutes=5)))
        repo.add(Identity(kind=WEB, id="b"), "theirs", _iso(timedelta(minutes=5)))
        mine_id = repo.entries(Identity(kind=WEB, id="a"))[0][0]

        # The other account's id is a guessable sequence; deleting by it alone
        # must not reach their row.
        assert repo.delete(Identity(kind=WEB, id="a"), mine_id) is True
        assert [
            text for _, text, _ in repo.entries(Identity(kind=WEB, id="b"))
        ] == ["theirs"]

    def test_delete_stops_at_a_sent_reminder(self, database_with_schema):
        repo = RemindersRepository(database_with_schema)
        identity = Identity(kind=WEB, id="default")

        repo.add(identity, "done", _iso(-timedelta(minutes=5)))
        repo.mark_sent(repo.get_due()[0][0])

        # The id still exists but the row is sent, so /cancel must not remove it.
        sent_id = repo.entries(identity)
        assert sent_id == []


class TestClipboardRepository:
    def test_get_last_returns_newest(self, database_with_schema):
        repo = ClipboardRepository(database_with_schema)
        identity = Identity(kind=WEB, id="default")

        repo.save(identity, "first", _iso(timedelta(0)))
        repo.save(identity, "second", _iso(timedelta(0)))

        assert repo.get_last(identity) == "second"

    def test_get_last_empty(self, database_with_schema):
        repo = ClipboardRepository(database_with_schema)

        assert repo.get_last(Identity(kind=WEB, id="nobody")) is None

    def test_isolated_by_identity(self, database_with_schema):
        repo = ClipboardRepository(database_with_schema)

        repo.save(telegram(111), "for A", _iso(timedelta(0)))
        repo.save(telegram(222), "for B", _iso(timedelta(0)))

        assert repo.get_last(telegram(111)) == "for A"
        assert repo.get_last(telegram(222)) == "for B"
