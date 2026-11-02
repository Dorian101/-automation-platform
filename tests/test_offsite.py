"""The copy of the nightly dump that leaves the host."""

import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.config import Config
from app.db import offsite
from app.db.backup import BackupManager
from app.db.links_repo import TelegramLinksRepository
from app.manage import main
from tests.conftest import TEST_USERNAME
from tests.test_backup import _dump_ok


def _fake_encrypt(source, passphrase, target_dir):
    target = target_dir / (source.name + ".gpg")
    target.write_bytes(b"sealed:" + source.read_bytes())
    return target


class TestRecipients:
    def test_only_linked_active_admins(self, create_user, users, database_with_schema):
        links = TelegramLinksRepository(database_with_schema)
        admin = users.get_by_username(TEST_USERNAME)
        users.set_admin(admin.id, True)
        links.link(admin.id, 111)

        unlinked = create_user(username="boss", password="x" * 12)
        users.set_admin(unlinked.id, True)

        plain = create_user(username="reader", password="x" * 12)
        links.link(plain.id, 222)

        assert offsite.recipients(users, links) == [111]

    def test_a_disabled_admin_is_left_out(
        self, create_user, users, database_with_schema
    ):
        links = TelegramLinksRepository(database_with_schema)
        admin = users.get_by_username(TEST_USERNAME)
        users.set_admin(admin.id, True)
        users.set_active(admin.id, False)
        links.link(admin.id, 111)

        assert offsite.recipients(users, links) == []


class TestSendCopy:
    def test_the_sealed_file_is_sent_and_then_removed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(offsite, "encrypt", _fake_encrypt)
        dump = tmp_path / "scheduled.sql"
        dump.write_bytes(b"-- dump")
        seen = {}

        async def send(token, chats, path):
            seen.update(token=token, chats=chats, path=path, body=path.read_bytes())

        name = offsite.send_copy(dump, "secret", "token", [1, 2], send=send)

        assert name == Path("scheduled.sql.gpg")
        assert seen["chats"] == [1, 2] and seen["token"] == "token"
        assert seen["body"] == b"sealed:-- dump"
        assert not seen["path"].exists()

    def test_nobody_to_send_to_is_an_error(self, tmp_path):
        dump = tmp_path / "scheduled.sql"
        dump.write_bytes(b"-- dump")

        with pytest.raises(RuntimeError, match="linked Telegram"):
            offsite.send_copy(dump, "secret", "token", [])


@pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg is not installed")
class TestEncrypt:
    def test_it_opens_with_the_passphrase_only(self, tmp_path):
        dump = tmp_path / "scheduled.sql"
        dump.write_bytes(b"-- secret notes")
        out = tmp_path / "out"
        out.mkdir()

        sealed = offsite.encrypt(dump, "correct horse", out)

        assert b"secret notes" not in sealed.read_bytes()

        def decrypt(passphrase):
            home = tmp_path / f"home-{len(passphrase)}"
            home.mkdir(mode=0o700)
            return subprocess.run(
                ["gpg", "--batch", "--homedir", str(home), "--pinentry-mode",
                 "loopback", "--passphrase-fd", "0", "-d", str(sealed)],
                input=passphrase.encode(), capture_output=True,
            )

        assert decrypt("correct horse").stdout == b"-- secret notes"
        assert decrypt("wrong").returncode != 0


class TestBackupCommand:
    @pytest.fixture
    def dumping(self, tmp_path, monkeypatch):
        manager = BackupManager(backups_dir=tmp_path)
        monkeypatch.setattr("app.manage.BackupManager", lambda: manager)
        monkeypatch.setattr("app.db.backup.subprocess.run", _dump_ok())
        monkeypatch.setattr("app.manage.recipients", lambda users, links: [111])
        return tmp_path

    def test_without_a_passphrase_nothing_leaves(
        self, dumping, monkeypatch, capsys
    ):
        monkeypatch.setattr(Config, "BACKUP_PASSPHRASE", "")
        monkeypatch.setattr(
            "app.manage.send_copy", lambda *a: pytest.fail("must not send")
        )

        assert main(["backup"]) == 0
        assert "Offsite copy off" in capsys.readouterr().out

    def test_with_a_passphrase_a_copy_is_sent(self, dumping, monkeypatch, capsys):
        monkeypatch.setattr(Config, "BACKUP_PASSPHRASE", "secret")
        calls = []
        monkeypatch.setattr(
            "app.manage.send_copy",
            lambda dump, passphrase, token, chats: calls.append(
                (dump, passphrase, chats)
            ) or Path(dump.name + ".gpg"),
        )

        assert main(["backup"]) == 0
        dump, passphrase, chats = calls[0]
        assert dump.exists() and passphrase == "secret" and chats == [111]
        assert "Offsite copy sent to 1 chat(s)" in capsys.readouterr().out

    def test_a_failed_copy_fails_the_run_and_keeps_the_dump(
        self, dumping, monkeypatch, capsys
    ):
        monkeypatch.setattr(Config, "BACKUP_PASSPHRASE", "secret")

        def broken(*args):
            raise RuntimeError("telegram is down")

        monkeypatch.setattr("app.manage.send_copy", broken)

        assert main(["backup"]) == 1
        assert "telegram is down" in capsys.readouterr().err
        assert len(list(dumping.glob("scheduled_*.sql"))) == 1
