from datetime import UTC, datetime, timedelta

from app import watch
from tests.conftest import TEST_USERNAME

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)


def _patch(monkeypatch, cert_days, domain_days, disk=0.5):
    async def cert(host):
        return NOW + timedelta(days=cert_days)

    async def domain(host):
        if domain_days is None:
            raise OSError("rdap is down")
        return NOW + timedelta(days=domain_days)

    monkeypatch.setattr(watch, "certificate_expiry", cert)
    monkeypatch.setattr(watch, "domain_expiry", domain)
    monkeypatch.setattr(watch, "disk_share", lambda: disk)


async def test_nothing_to_say_when_everything_is_far(monkeypatch):
    _patch(monkeypatch, 60, 300)

    assert await watch.problems("example.com", NOW) == []
    assert watch.LAST["cert"].endswith("через 60 дн.")


async def test_each_problem_is_named(monkeypatch):
    _patch(monkeypatch, 10, 20, disk=0.85)

    found = await watch.problems("example.com", NOW)

    assert len(found) == 3
    assert found[0] == "Сертификат example.com истекает через 10 дн."
    assert found[1] == "Домен example.com истекает через 20 дн."
    assert found[2] == "Диск занят на 85%"


async def test_an_unreachable_registry_is_not_a_warning(monkeypatch):
    _patch(monkeypatch, 60, None)

    assert await watch.problems("example.com", NOW) == []
    assert watch.LAST["domain"] == "не проверено"


def test_the_next_check_is_the_next_morning(monkeypatch):
    monkeypatch.setattr(watch.Config, "DISPLAY_TIMEZONE", "Europe/Moscow")

    before = NOW - timedelta(hours=1)
    assert watch.next_check(before).isoformat() == "2026-11-10T09:00:00+03:00"
    assert watch.next_check(NOW).isoformat() == "2026-11-11T09:00:00+03:00"


async def test_warnings_reach_every_admin(monkeypatch, create_user, users):
    _patch(monkeypatch, 5, 300)
    sent = []

    class Notifier:
        async def deliver(self, target, text):
            sent.append((target, text))

    users.set_admin(users.get_by_username(TEST_USERNAME).id, True)
    checker = watch.Watch(Notifier(), users, "example.com")

    await checker._tell_admins("\n".join(await watch.problems("example.com", NOW)))

    assert len(sent) == 1
    assert "Сертификат example.com истекает через 5 дн." in sent[0][1]


async def test_a_failing_check_does_not_stop_the_watch(monkeypatch, users):
    async def broken(host, now):
        raise RuntimeError("database is down")

    monkeypatch.setattr(watch, "problems", broken)

    await watch.Watch(None, users, "example.com")._check(notify=True)
