"""Гонки, перезапуски, квота и неизменность идемпотентных результатов."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from brief_intake.service import safe_result
from brief_intake.storage import DatabaseLock, OperationConflict, QuotaExceeded, Store


def make_store(path: Path) -> Store:
    store = Store(path)
    store.initialize()
    return store


def test_simultaneous_duplicate_has_one_reservation(tmp_path: Path) -> None:
    store = make_store(tmp_path / "db.sqlite3")

    def reserve(_: int) -> str:
        try:
            store.reserve("same", "text", "model", 100)
            return "reserved"
        except OperationConflict as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert results.count("reserved") == 1
    assert results.count("operation_processing") == 7


def test_parallel_quota_is_not_exceeded(tmp_path: Path) -> None:
    store = make_store(tmp_path / "db.sqlite3")

    def reserve(index: int) -> bool:
        try:
            store.reserve(str(index), "text", "model", 3)
            return True
        except QuotaExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(12))) == 3


def test_replay_quota_and_next_day(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    store = make_store(path)
    today = datetime(2026, 11, 6, 23, 59, tzinfo=UTC)
    initial = store.reserve("one", "same", "model", 1, today)
    store.complete(initial.record.id, safe_result("provider_timeout"), None, "provider_timeout")
    with pytest.raises(QuotaExceeded):
        store.reserve("two", "new", "model", 1, today)
    restarted = make_store(path)
    assert restarted.reserve("one", "same", "model", 1, today).replayed
    with pytest.raises(OperationConflict, match="idempotency_conflict"):
        restarted.reserve("one", "changed", "model", 1, today)
    assert not restarted.reserve("two", "new", "model", 1, today + timedelta(minutes=2)).replayed


def test_interrupted_request_is_not_reprocessed(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    store = make_store(path)
    record = store.reserve("one", "text", "model", 100).record
    restarted = make_store(path)
    restored = restarted.get(record.id)
    assert restored is not None
    assert restored.processing_state == "interrupted"
    assert restored.result is not None and restored.result.escalate
    with pytest.raises(OperationConflict, match="operation_interrupted"):
        restarted.reserve("one", "text", "model", 100)


def test_second_instance_cannot_recover_active_request(tmp_path: Path) -> None:
    first = DatabaseLock(tmp_path / "db.sqlite3")
    second = DatabaseLock(tmp_path / "db.sqlite3")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="другим экземпляром"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
