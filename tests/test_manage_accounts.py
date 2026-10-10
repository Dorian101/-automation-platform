"""The account commands on the command line.

Disabling, re-enabling, resetting a password and revoking sessions are what an
administrator reaches for when a credential has leaked. They run from the host
with no browser involved, and for the same reason `create-user` does, a password
is never passed as an argument.
"""

import pytest

from app.db.sessions_repo import SessionsRepository
from app.db.users_repo import UsersRepository
from app.manage import main
from tests.conftest import TEST_PASSWORD, TEST_USERNAME

NEW_PASSWORD = "a-brand-new-password"


@pytest.fixture
def cli(database_with_schema, create_user):
    """Run the commands against the test schema, as the real thing does."""
    def _run(*argv: str) -> int:
        return main(list(argv))

    return _run


@pytest.fixture
def stored(database_with_schema):
    return (
        UsersRepository(database_with_schema),
        SessionsRepository(database_with_schema),
    )


def _give_a_session(stored, username=TEST_USERNAME) -> str:
    users, sessions = stored
    user = users.get_by_username(username)

    return sessions.create(user.id, 3600)


class TestDisableUser:
    def test_disables_the_account(self, cli, stored):
        assert cli("disable-user", TEST_USERNAME) == 0

        users, _ = stored
        assert users.get_by_username(TEST_USERNAME).is_active is False

    def test_a_disabled_account_cannot_sign_in(self, cli, stored):
        cli("disable-user", TEST_USERNAME)

        users, _ = stored
        assert users.authenticate(TEST_USERNAME, TEST_PASSWORD) is None

    def test_it_revokes_the_accounts_sessions(self, cli, stored):
        _, sessions = stored
        token = _give_a_session(stored)

        cli("disable-user", TEST_USERNAME)

        assert sessions.get_user_id(token) is None

    def test_an_unknown_account_fails(self, cli, capsys):
        assert cli("disable-user", "nobody") == 1

        assert "no user 'nobody'" in capsys.readouterr().err

    def test_disabling_twice_is_harmless(self, cli, capsys):
        cli("disable-user", TEST_USERNAME)
        capsys.readouterr()

        assert cli("disable-user", TEST_USERNAME) == 0
        assert "already disabled" in capsys.readouterr().out

    def test_the_action_is_logged(self, cli, caplog):
        with caplog.at_level("INFO", logger="manage"):
            cli("disable-user", TEST_USERNAME)

        assert "Disabled web account" in caplog.text


class TestEnableUser:
    def test_enables_the_account(self, cli, stored):
        cli("disable-user", TEST_USERNAME)

        assert cli("enable-user", TEST_USERNAME) == 0

        users, _ = stored
        assert users.get_by_username(TEST_USERNAME).is_active is True

    def test_sign_in_works_again(self, cli, stored):
        cli("disable-user", TEST_USERNAME)
        cli("enable-user", TEST_USERNAME)

        users, _ = stored
        assert users.authenticate(TEST_USERNAME, TEST_PASSWORD) is not None

    def test_an_unknown_account_fails(self, cli, capsys):
        assert cli("enable-user", "nobody") == 1

        assert "no user 'nobody'" in capsys.readouterr().err

    def test_enabling_an_active_account_is_harmless(self, cli, capsys):
        assert cli("enable-user", TEST_USERNAME) == 0
        assert "already active" in capsys.readouterr().out


class TestResetPassword:
    def test_replaces_the_password(self, cli, stored, monkeypatch):
        monkeypatch.setattr("app.manage._read_password", lambda: NEW_PASSWORD)

        assert cli("reset-password", TEST_USERNAME) == 0

        users, _ = stored
        assert users.authenticate(TEST_USERNAME, NEW_PASSWORD) is not None
        assert users.authenticate(TEST_USERNAME, TEST_PASSWORD) is None

    def test_it_revokes_the_accounts_sessions(self, cli, stored, monkeypatch):
        token = _give_a_session(stored)
        monkeypatch.setattr("app.manage._read_password", lambda: NEW_PASSWORD)

        cli("reset-password", TEST_USERNAME)

        _, sessions = stored
        assert sessions.get_user_id(token) is None

    def test_a_short_password_is_refused(self, cli, stored, monkeypatch):
        monkeypatch.setattr("app.manage._read_password", lambda: "short")

        assert cli("reset-password", TEST_USERNAME) == 1

        users, _ = stored
        assert users.authenticate(TEST_USERNAME, TEST_PASSWORD) is not None

    def test_an_unknown_account_is_rejected_before_reading_a_password(
        self,
        cli,
        monkeypatch,
    ):
        def _explode():
            raise AssertionError("a password must not be read for an unknown user")

        monkeypatch.setattr("app.manage._read_password", _explode)

        assert cli("reset-password", "nobody") == 1


class TestRevokeSessions:
    def test_removes_every_session_of_the_account(self, cli, stored):
        first = _give_a_session(stored)
        second = _give_a_session(stored)

        assert cli("revoke-sessions", TEST_USERNAME) == 0

        _, sessions = stored
        assert sessions.get_user_id(first) is None
        assert sessions.get_user_id(second) is None

    def test_it_leaves_another_accounts_sessions_alone(self, cli, stored, create_user):
        create_user("bob")
        bob_token = _give_a_session(stored, "bob")
        _give_a_session(stored)

        cli("revoke-sessions", TEST_USERNAME)

        _, sessions = stored
        assert sessions.get_user_id(bob_token) is not None

    def test_an_account_without_sessions_is_harmless(self, cli, capsys):
        assert cli("revoke-sessions", TEST_USERNAME) == 0
        assert "0 session(s) revoked" in capsys.readouterr().out

    def test_an_unknown_account_fails(self, cli, capsys):
        assert cli("revoke-sessions", "nobody") == 1

        assert "no user 'nobody'" in capsys.readouterr().err
