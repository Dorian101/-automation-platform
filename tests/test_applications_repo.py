from datetime import date, timedelta

import pytest

from app.core.identity import WEB, Identity
from app.db.applications_repo import ApplicationsRepository

OTHER = Identity(kind=WEB, id="someone-else")
OPEN = ("applied", "answered", "interview", "offer", "rejected")


@pytest.fixture
def repo(database_with_schema):
    return ApplicationsRepository(database_with_schema)


def _days_ago(days: int) -> date:
    return date.today() - timedelta(days=days)


class TestAdd:
    def test_starts_at_the_first_stage(self, repo, web_identity):
        repo.add(web_identity, "Ромашка", "BI-аналитик", _days_ago(1))

        assert repo.entries(web_identity)[0]["status"] == "applied"

    def test_keeps_the_url_and_note(self, repo, web_identity):
        repo.add(
            web_identity,
            "Ромашка",
            "BI-аналитик",
            _days_ago(1),
            url="https://hh.ru/vacancy/1",
            note="через знакомого",
        )

        entry = repo.entries(web_identity)[0]

        assert entry["url"] == "https://hh.ru/vacancy/1"
        assert entry["note"] == "через знакомого"

    def test_orders_newest_first(self, repo, web_identity):
        repo.add(web_identity, "Старая", "BI", _days_ago(30))
        repo.add(web_identity, "Новая", "BI", _days_ago(1))

        assert [e["company"] for e in repo.entries(web_identity)] == [
            "Новая",
            "Старая",
        ]


class TestStatus:
    def test_an_unknown_stage_cannot_be_stored(self, repo, web_identity):
        """The CHECK is the rule, not the plugin.

        A status reaching the database by any other path — a management
        command, a future import — still cannot be one that does not exist.
        """
        application_id = repo.add(web_identity, "Ромашка", "BI", _days_ago(1))

        with pytest.raises(Exception):
            repo.set_status(web_identity, application_id, "hired")

    def test_every_known_stage_is_accepted(self, repo, web_identity):
        """The CHECK lists five stages; a sixth would break every funnel."""
        for index, stage in enumerate(OPEN):
            application_id = repo.add(
                web_identity, f"Компания {index}", "BI", _days_ago(index + 1)
            )
            repo.set_status(web_identity, application_id, stage)

        assert len(repo.entries(web_identity)) == len(OPEN)

    def test_status_change_is_scoped_to_the_account(self, repo, web_identity):
        """The id is a global sequence; a guess must not reach another's."""
        application_id = repo.add(OTHER, "Чужая", "BI", _days_ago(1))

        assert repo.set_status(web_identity, application_id, "answered") is False
        assert repo.entries(OTHER)[0]["status"] == "applied"
        assert repo.set_status(OTHER, application_id, "answered") is True


class TestIsolation:
    def test_one_account_does_not_see_anothers(self, repo, web_identity):
        repo.add(web_identity, "Моя", "BI", _days_ago(1))
        repo.add(OTHER, "Чужая", "BI", _days_ago(1))

        assert [e["company"] for e in repo.entries(web_identity)] == ["Моя"]
        assert [e["company"] for e in repo.entries(OTHER)] == ["Чужая"]

    def test_delete_is_scoped_to_the_account(self, repo, web_identity):
        application_id = repo.add(web_identity, "Моя", "BI", _days_ago(1))

        assert repo.delete(OTHER, application_id) is False
        assert repo.delete(web_identity, application_id) is True

    def test_counts_are_per_account(self, repo, web_identity):
        mine = repo.add(web_identity, "Моя", "BI", _days_ago(1))
        repo.add(OTHER, "Чужая", "BI", _days_ago(1))
        repo.set_status(web_identity, mine, "answered")

        assert repo.counts_by_status(web_identity) == {"answered": 1}
        assert repo.counts_by_status(OTHER) == {"applied": 1}


class TestWidening:
    """A linked pair is read as one person, the rule every read follows."""

    def test_counts_span_the_pair(self, repo):
        web = Identity(kind=WEB, id="alexey")
        chat = Identity(kind="telegram", id="555")

        application_id = repo.add(web, "Ромашка", "BI", _days_ago(1))
        repo.set_status(web, application_id, "interview")

        assert repo.counts_by_status([web, chat]) == {"interview": 1}

    def test_an_unpaired_identity_resolves_to_itself(self, repo, web_identity):
        repo.add(web_identity, "Моя", "BI", _days_ago(1))

        assert repo.counts_by_status(web_identity) == {"applied": 1}
