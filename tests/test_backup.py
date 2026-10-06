import datetime as std_datetime
import os
import re
import subprocess
import time

import pytest

from app.core.config import Config
from app.db.backup import BackupManager, default_backups_dir
from app.manage import main

DUMP_NAME = r"scheduled_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.sql"


def _dump_ok(payload=b"-- dumped\n"):
    def run(cmd, **kwargs):
        kwargs["stdout"].write(payload)
        return subprocess.CompletedProcess(cmd, 0, stderr=b"")

    return run


def _dump_fails(message=b"pg_dump: error: connection to server failed"):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stderr=message)

    return run


def _age(path, days):
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


class _FrozenClock:
    @staticmethod
    def now():
        return std_datetime.datetime(2026, 10, 6, 12, 0, 0)


def test_create_backup_writes_named_file(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.backup.subprocess.run", _dump_ok())
    manager = BackupManager(backups_dir=tmp_path)

    path = manager.create_backup("scheduled")

    assert path.parent == tmp_path
    assert re.fullmatch(DUMP_NAME, path.name)
    assert path.read_bytes() == b"-- dumped\n"


def test_backup_file_is_not_world_readable(tmp_path, monkeypatch):
    """The dump carries scrypt password hashes."""
    monkeypatch.setattr("app.db.backup.subprocess.run", _dump_ok())
    manager = BackupManager(backups_dir=tmp_path)

    path = manager.create_backup("scheduled")

    assert path.stat().st_mode & 0o077 == 0


def test_failed_dump_raises_and_removes_the_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.backup.subprocess.run", _dump_fails())
    manager = BackupManager(backups_dir=tmp_path)

    with pytest.raises(RuntimeError, match="connection to server failed"):
        manager.create_backup("scheduled")

    assert list(tmp_path.glob("*.sql")) == []


def test_missing_pg_dump_is_reported_and_leaves_no_file(tmp_path, monkeypatch):
    def run(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "pg_dump")

    monkeypatch.setattr("app.db.backup.subprocess.run", run)
    manager = BackupManager(backups_dir=tmp_path)

    with pytest.raises(RuntimeError, match="could not run pg_dump"):
        manager.create_backup("scheduled")

    assert list(tmp_path.glob("*.sql")) == []


def test_two_backups_in_one_second_do_not_share_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.backup.subprocess.run", _dump_ok())
    monkeypatch.setattr("app.db.backup.datetime", _FrozenClock)
    manager = BackupManager(backups_dir=tmp_path)

    first = manager.create_backup("scheduled")

    with pytest.raises(RuntimeError, match="already exists"):
        manager.create_backup("scheduled")

    assert first.exists()
    assert first.read_bytes() == b"-- dumped\n"


def test_prune_drops_expired_and_keeps_recent(tmp_path):
    manager = BackupManager(backups_dir=tmp_path)
    expired = tmp_path / "scheduled_2020-01-01_00-00-00.sql"
    recent = tmp_path / "scheduled_2099-01-01_00-00-00.sql"
    expired.write_text("-- old")
    recent.write_text("-- new")
    _age(expired, days=30)

    removed = manager.prune(retention_days=14)

    assert removed == [expired]
    assert not expired.exists()
    assert recent.exists()


def test_prune_disabled_keeps_expired_files(tmp_path):
    """Zero must mean keep everything, not delete everything."""
    manager = BackupManager(backups_dir=tmp_path)
    expired = tmp_path / "scheduled_2020-01-01_00-00-00.sql"
    expired.write_text("-- old")
    _age(expired, days=3000)

    assert manager.prune(retention_days=0) == []
    assert expired.exists()


def test_prune_without_a_backup_directory_is_not_an_error(tmp_path):
    manager = BackupManager(backups_dir=tmp_path / "never-created")

    assert manager.prune(retention_days=14) == []


def test_default_dir_follows_config(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "BACKUP_DIR", str(tmp_path))

    assert default_backups_dir() == tmp_path


def test_default_dir_falls_back_to_the_checkout(monkeypatch):
    monkeypatch.setattr(Config, "BACKUP_DIR", "")

    assert default_backups_dir().name == "backups"


def test_backup_command_writes_and_prunes(tmp_path, monkeypatch, capsys):
    manager = BackupManager(backups_dir=tmp_path)
    monkeypatch.setattr("app.manage.BackupManager", lambda: manager)
    monkeypatch.setattr("app.db.backup.subprocess.run", _dump_ok())
    monkeypatch.setattr(Config, "BACKUP_RETENTION_DAYS", 14)

    expired = tmp_path / "scheduled_2020-01-01_00-00-00.sql"
    expired.write_text("-- old")
    _age(expired, days=365)

    assert main(["backup"]) == 0

    output = capsys.readouterr().out
    assert "Backup written:" in output
    assert "Pruned 1 expired backup(s)" in output
    assert not expired.exists()
    assert len(list(tmp_path.glob("scheduled_*.sql"))) == 1


def test_backup_command_reports_a_failed_dump(tmp_path, monkeypatch, capsys):
    manager = BackupManager(backups_dir=tmp_path)
    monkeypatch.setattr("app.manage.BackupManager", lambda: manager)
    monkeypatch.setattr("app.db.backup.subprocess.run", _dump_fails())

    assert main(["backup"]) == 1
    assert "backup failed" in capsys.readouterr().err
