"""Backup работающей WAL-базы и защита от перезаписи копии."""

import sqlite3
from pathlib import Path

import pytest

from brief_intake.backup import create_backup
from brief_intake.storage import Store


def test_backup_contains_committed_card(tmp_path: Path) -> None:
    source = tmp_path / "active.sqlite3"
    store = Store(source)
    store.initialize()
    record = store.reserve("one", "text", "model", 100).record
    target = tmp_path / "backups" / "copy.sqlite3"
    create_backup(source, target)
    copied = Store(target).get(record.id)
    assert copied == record
    with sqlite3.connect(target) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(FileExistsError):
        create_backup(source, target)
    assert Store(target).get(record.id) == record


def test_missing_source_does_not_create_empty_database(tmp_path: Path) -> None:
    source, target = tmp_path / "missing.sqlite3", tmp_path / "copy.sqlite3"
    with pytest.raises(ValueError):
        create_backup(source, target)
    assert not source.exists()
    assert not target.exists()
