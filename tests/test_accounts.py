import pytest
from psycopg.errors import UniqueViolation

from app.core.identity import WEB, Identity
from app.core.passwords import PasswordError
from app.db.notes_repo import NotesRepository
from app.db.sessions_repo import SessionsRepository
from tests.conftest import TEST_USERNAME

PASSWORD = "correct-horse-battery"


def _texts(entries):
    """The note text alone, so assertions do not repeat the row id."""
    return [text for _, text in entries]


@pytest.fixture
def sessions(database_with_schema):
    return SessionsRepository(database_with_schema)


@pytest.fixture
def notes(database_with_schema):
    return NotesRepository(database_with_schema)


class TestUsers:
    def test_create_returns_the_account(self, users):
        user = users.create("alexey", PASSWORD)

        assert user.id > 0
        assert user.username == "alexey"
        assert user.is_active is True

    def test_authenticate_accepts_the_right_password(self, users):
        users.create("alexey", PASSWORD)

        assert users.authenticate("alexey", PASSWORD) is not None

    def test_authenticate_rejects_a_wrong_password(self, users):
        users.create("alexey", PASSWORD)

        assert users.authenticate("alexey", "not-the-password") is None

    def test_authenticate_rejects_an_unknown_user(self, users):
        assert users.authenticate("nobody-here", PASSWORD) is None

    def test_password_is_not_stored_in_clear_text(self, users, database_with_schema):
        user = users.create("alexey", PASSWORD)

        with database_with_schema.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT password_hash FROM users WHERE id = %s", (user.id,))
                stored = cur.fetchone()[0]

        assert PASSWORD not in stored
        assert stored.startswith("scrypt$")

    def test_username_is_case_insensitive(self, users):
        users.create("alexey", PASSWORD)

        assert users.authenticate("ALEXEY", PASSWORD) is not None
        assert users.get_by_username("Alexey") is not None

    def test_username_keeps_its_casing_for_display(self, users):
        user = users.create("Alexey", PASSWORD)

        assert user.username == "Alexey"

    def test_duplicate_username_is_rejected_regardless_of_case(self, users):
        users.create("alexey", PASSWORD)

        with pytest.raises(UniqueViolation):
            users.create("ALEXEY", "another-password")

    def test_empty_username_is_rejected(self, users):
        with pytest.raises(ValueError, match="must not be empty"):
            users.create("   ", PASSWORD)

    def test_short_password_is_rejected(self, users):
        with pytest.raises(PasswordError):
            users.create("alexey", "short")
    def test_disabled_account_cannot_sign_in(self, users, database_with_schema):
        user = users.create("alexey", PASSWORD)

        with database_with_schema.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET is_active = FALSE WHERE id = %s",
                    (user.id,),
                )
            conn.commit()

        assert users.authenticate("alexey", PASSWORD) is None

    def test_list_all_reports_every_account(self, users, create_user):
        create_user("alice")
        create_user("bob")

        assert {user.username for user in users.list_all()} == {
            TEST_USERNAME,
            "alice",
            "bob",
        }

    def test_identity_carries_the_username(self, users):
        user = users.create("alexey", PASSWORD)

        assert users.identity_for(user) == Identity(kind=WEB, id="alexey")

    def test_set_active_disables_the_account(self, users):
        user = users.create("alexey", PASSWORD)

        assert users.set_active(user.id, False) is True
        assert users.get_by_username("alexey").is_active is False
        assert users.authenticate("alexey", PASSWORD) is None

    def test_set_active_can_enable_again(self, users):
        user = users.create("alexey", PASSWORD)
        users.set_active(user.id, False)

        assert users.set_active(user.id, True) is True
        assert users.authenticate("alexey", PASSWORD) is not None

    def test_set_active_on_an_unknown_user_changes_nothing(self, users):
        assert users.set_active(999999, False) is False

    def test_set_password_replaces_the_old_one(self, users):
        user = users.create("alexey", PASSWORD)

        users.set_password(user.id, "a-brand-new-password")

        assert users.authenticate("alexey", "a-brand-new-password") is not None
        assert users.authenticate("alexey", PASSWORD) is None

    def test_set_password_rejects_a_short_one(self, users):
        user = users.create("alexey", PASSWORD)

        with pytest.raises(PasswordError):
            users.set_password(user.id, "short")

        assert users.authenticate("alexey", PASSWORD) is not None


class TestSessions:
    def test_round_trip(self, users, sessions):
        user = users.create("alexey", PASSWORD)

        token = sessions.create(user.id, 3600)

        assert sessions.get_user_id(token) == user.id

    def test_unknown_token_is_rejected(self, sessions):
        assert sessions.get_user_id("not-a-real-token") is None

    def test_empty_token_is_rejected(self, sessions):
        assert sessions.get_user_id("") is None

    def test_raw_token_is_never_stored(self, users, sessions, database_with_schema):
        user = users.create("alexey", PASSWORD)
        token = sessions.create(user.id, 3600)

        with database_with_schema.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM sessions WHERE token_hash = %s",
                    (token,),
                )
                stored_raw = cur.fetchone()[0]

                cur.execute("SELECT count(*) FROM sessions")
                total = cur.fetchone()[0]

        assert stored_raw == 0
        assert total == 1

    def test_delete_ends_the_session(self, users, sessions):
        user = users.create("alexey", PASSWORD)
        token = sessions.create(user.id, 3600)

        sessions.delete(token)

        assert sessions.get_user_id(token) is None

    def test_delete_of_an_unknown_token_is_harmless(self, sessions):
        sessions.delete("nothing-here")
        sessions.delete("")

    def test_expired_session_is_not_accepted(
        self,
        users,
        sessions,
        database_with_schema,
    ):
        user = users.create("alexey", PASSWORD)
        token = sessions.create(user.id, 3600)

        with database_with_schema.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sessions SET expires_at = "
                    "CURRENT_TIMESTAMP - interval '1 day'"
                )
            conn.commit()

        assert sessions.get_user_id(token) is None

    def test_delete_expired_removes_stale_rows(
        self,
        users,
        sessions,
        database_with_schema,
    ):
        user = users.create("alexey", PASSWORD)
        sessions.create(user.id, 3600)

        with database_with_schema.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sessions SET expires_at = "
                    "CURRENT_TIMESTAMP - interval '1 day'"
                )
            conn.commit()

        assert sessions.delete_expired() == 1

        with database_with_schema.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM sessions")
                assert cur.fetchone()[0] == 0

    def test_removing_the_user_removes_its_sessions(
        self,
        users,
        sessions,
        database_with_schema,
    ):
        user = users.create("alexey", PASSWORD)
        token = sessions.create(user.id, 3600)

        with database_with_schema.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE id = %s", (user.id,))
            conn.commit()

        assert sessions.get_user_id(token) is None

    def test_delete_for_user_removes_only_that_users_sessions(
        self,
        users,
        sessions,
    ):
        alice = users.create("alice", PASSWORD)
        bob = users.create("bob", PASSWORD)
        alice_token = sessions.create(alice.id, 3600)
        bob_token = sessions.create(bob.id, 3600)

        assert sessions.delete_for_user(alice.id) == 1

        assert sessions.get_user_id(alice_token) is None
        assert sessions.get_user_id(bob_token) == bob.id

    def test_delete_for_user_without_sessions_removes_nothing(self, users, sessions):
        user = users.create("alexey", PASSWORD)

        assert sessions.delete_for_user(user.id) == 0


class TestIsolation:
    """The whole point of accounts: one user must never see another's data."""

    def test_web_users_have_separate_notes(self, notes, create_user):
        create_user("alice")
        create_user("bob")

        notes.add(Identity(kind=WEB, id="alice"), "alice secret")
        notes.add(Identity(kind=WEB, id="bob"), "bob secret")

        assert _texts(notes.entries(Identity(kind=WEB, id="alice"))) == ["alice secret"]
        assert _texts(notes.entries(Identity(kind=WEB, id="bob"))) == ["bob secret"]

    def test_web_user_does_not_see_telegram_data(self, notes):
        from app.core.identity import telegram

        notes.add(telegram(42), "telegram note")

        assert _texts(notes.entries(Identity(kind=WEB, id="alice"))) == []
