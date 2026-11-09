"""SQLite: атомарная защита от повторов и квота одного экземпляра сервиса."""

import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from brief_intake.models import RequestRecord, Result, TokenUsage
from brief_intake.service import safe_result


class OperationConflict(Exception):
    """Идентификатор занят другим текстом или незавершённой операцией."""

    def __init__(self, code: str, record_id: str) -> None:
        super().__init__(code)
        self.code = code
        self.record_id = record_id


class QuotaExceeded(Exception):
    """Новый вызов провайдера запрещён до следующих суток UTC."""


@dataclass(frozen=True)
class Reservation:
    """Новая запись либо ранее завершённая карточка."""

    record: RequestRecord
    replayed: bool


class DatabaseLock:
    """Не разрешать двум процессам обслуживать одну SQLite одновременно."""

    def __init__(self, database_path: Path) -> None:
        self.path = database_path.with_suffix(database_path.suffix + ".lock")
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        """Удерживать системную блокировку до остановки процесса."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open("a+b")
        try:
            if file.tell() == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            file.close()
            raise RuntimeError("Эта база уже используется другим экземпляром приложения") from None
        self._file = file

    def release(self) -> None:
        """Закрытие дескриптора освобождает блокировку, включая аварийный выход ОС."""
        if self._file is not None:
            self._file.close()
            self._file = None


class Store:
    """Короткие соединения и транзакции; HTTP-запрос не держит блокировку БД."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        """Создать схему и пометить прерванные операции без повторного вызова LLM."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,
                    operation_key TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    processing_state TEXT NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    error_type TEXT,
                    model TEXT NOT NULL,
                    usage_json TEXT
                );
                CREATE TABLE IF NOT EXISTS daily_calls (
                    day TEXT PRIMARY KEY,
                    count INTEGER NOT NULL CHECK (count >= 0)
                );
            """)
            connection.execute(
                """UPDATE requests SET processing_state='interrupted', updated_at=?,
                   error_code='processing_interrupted', error_type='ProcessInterrupted',
                   result_json=?
                   WHERE processing_state='processing'""",
                (
                    datetime.now(UTC).isoformat(),
                    safe_result("processing_interrupted").model_dump_json(),
                ),
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> RequestRecord:
        return RequestRecord(
            id=row["id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            processing_state=row["processing_state"],
            text=row["text"],
            result=Result.model_validate_json(row["result_json"]) if row["result_json"] else None,
            error_code=row["error_code"],
            error_type=row["error_type"],
            model=row["model"],
            usage=TokenUsage.model_validate_json(row["usage_json"]) if row["usage_json"] else None,
        )

    def reserve(
        self, operation_key: str, text: str, model: str, limit: int, now: datetime | None = None
    ) -> Reservation:
        """Атомарно закрепить ключ и суточный вызов либо вернуть сохранённый результат."""
        timestamp = now or datetime.now(UTC)
        day = timestamp.astimezone(UTC).date().isoformat()
        stamp = timestamp.isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                previous = connection.execute(
                    "SELECT * FROM requests WHERE operation_key=?", (operation_key,)
                ).fetchone()
                if previous is not None:
                    record = self._record(previous)
                    if record.text != text:
                        raise OperationConflict("idempotency_conflict", record.id)
                    if record.processing_state != "completed":
                        raise OperationConflict(f"operation_{record.processing_state}", record.id)
                    connection.commit()
                    return Reservation(record, replayed=True)

                quota = connection.execute(
                    "SELECT count FROM daily_calls WHERE day=?", (day,)
                ).fetchone()
                if quota is not None and quota["count"] >= limit:
                    raise QuotaExceeded
                record_id = str(uuid4())
                connection.execute(
                    """INSERT INTO requests
                       (id, operation_key, text, created_at, updated_at, processing_state, model)
                       VALUES (?, ?, ?, ?, ?, 'processing', ?)""",
                    (record_id, operation_key, text, stamp, stamp, model),
                )
                connection.execute(
                    """INSERT INTO daily_calls(day, count) VALUES (?, 1)
                       ON CONFLICT(day) DO UPDATE SET count=count+1""",
                    (day,),
                )
                row = connection.execute(
                    "SELECT * FROM requests WHERE id=?", (record_id,)
                ).fetchone()
                assert row is not None
                record = self._record(row)
                connection.commit()
                return Reservation(record, replayed=False)
            except BaseException:
                connection.rollback()
                raise

    def complete(
        self,
        record_id: str,
        result: Result,
        usage: TokenUsage | None,
        error_code: str | None = None,
        error_type: str | None = None,
    ) -> RequestRecord:
        """Сохранить итог до успешного HTTP-ответа."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """UPDATE requests SET processing_state='completed', updated_at=?, result_json=?,
                   usage_json=?, error_code=?, error_type=?
                   WHERE id=? AND processing_state='processing'
                   RETURNING *""",
                (
                    datetime.now(UTC).isoformat(),
                    result.model_dump_json(),
                    usage.model_dump_json() if usage is not None else None,
                    error_code,
                    error_type,
                    record_id,
                ),
            )
            row = cursor.fetchone()
            cursor.close()
            if row is None:
                raise sqlite3.IntegrityError("Не найдена активная операция для сохранения")
            return self._record(row)

    def get(self, record_id: str) -> RequestRecord | None:
        """Прочитать карточку вместе с исходником и безопасной диагностикой."""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM requests WHERE id=?", (record_id,)).fetchone()
            return self._record(row) if row is not None else None

    def check(self) -> None:
        """Проверить доступность схемы для health endpoint."""
        with closing(self._connect()) as connection:
            connection.execute("SELECT id FROM requests LIMIT 1").fetchone()

    def audit(self, limit: int = 10) -> list[dict[str, object]]:
        """Вернуть краткий аудит без исходников и черновиков для локальной команды."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, created_at, processing_state, result_json,
                   error_code, model, usage_json
                   FROM requests ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "processing_state": row["processing_state"],
                "status": json.loads(row["result_json"])["status"] if row["result_json"] else None,
                "error_code": row["error_code"],
                "model": row["model"],
                "usage": json.loads(row["usage_json"]) if row["usage_json"] else None,
            }
            for row in rows
        ]
