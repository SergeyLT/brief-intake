"""Структурированные диагностические события без пользовательского содержимого."""

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from types import TracebackType

from brief_intake.config import Settings

ALLOWED_FIELDS = {
    "operation_id",
    "route",
    "http_status",
    "duration_ms",
    "status",
    "replayed",
    "error_category",
    "error_code",
    "error_type",
    "component",
    "operation",
    "stage",
    "retryable",
    "retries_exhausted",
    "frames",
}


def safe_frames(tb: TracebackType | None) -> list[dict[str, str | int]]:
    """Сохранить место ошибки без сообщений, исходного кода и локальных переменных."""
    return [
        {
            "file": frame.filename.replace("\\", "/").rsplit("/", 1)[-1],
            "function": frame.name,
            "line": frame.lineno or 0,
        }
        for frame in traceback.extract_tb(tb)[-5:]
    ]


class EventLogger:
    """Выделенный stdout logger с whitelist, не затрагивающий сторонние handlers."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.Logger("brief_intake.events", logging.INFO)
        self.logger.propagate = False
        if not self.logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)

    def emit(
        self, event: str, message: str, request_id: str, level: int = logging.INFO, **fields: object
    ) -> None:
        """Пропустить только разрешённый контекст и маскировать известные ключи."""
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": logging.getLevelName(level),
            "service": "brief-intake",
            "environment": self.settings.environment,
            "version": self.settings.version,
            "logger": "brief_intake.events",
            "event": event,
            "message": message,
            "request_id": request_id,
            **{key: value for key, value in fields.items() if key in ALLOWED_FIELDS},
        }
        serialized = json.dumps(payload, ensure_ascii=False)
        for secret in (self.settings.api_key, self.settings.proxyapi_key):
            serialized = serialized.replace(
                json.dumps(secret, ensure_ascii=False)[1:-1], "[СКРЫТО]"
            )
        self.logger.log(level, serialized)
