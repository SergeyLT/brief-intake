"""Ограничение HTTP-тела и безопасная диагностика запросов."""

import logging
import sqlite3
from time import monotonic
from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from brief_intake.logging_config import EventLogger, safe_frames


class RequestGuard:
    """Не буферизовать неограниченный ввод и не раскрывать исключения клиенту."""

    def __init__(self, app: ASGIApp, events: EventLogger, max_body_bytes: int) -> None:
        self.app = app
        self.events = events
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Обработать один ASGI-запрос со служебным идентификатором."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = monotonic()
        http_status = 500
        response_started = False

        async def send_response(message: Message) -> None:
            nonlocal http_status, response_started
            if message["type"] == "http.response.start":
                http_status = message["status"]
                response_started = True
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            if scope["method"] == "POST" and scope["path"] == "/ingest":
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > self.max_body_bytes:
                        await JSONResponse(
                            {
                                "error": {
                                    "code": "body_too_large",
                                    "message": "Тело запроса слишком большое",
                                }
                            },
                            status_code=413,
                        )(scope, receive, send_response)
                        return
                    if not message.get("more_body", False):
                        break
                delivered = False

                async def replay_body() -> Message:
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                await self.app(scope, replay_body, send_response)
            else:
                await self.app(scope, receive, send_response)
        except Exception as exc:
            is_database = isinstance(exc, sqlite3.Error)
            code = "storage_unavailable" if is_database else "unexpected_internal_error"
            self.events.emit(
                "request_failed",
                "Запрос не выполнен",
                request_id,
                logging.ERROR,
                error_category="database" if is_database else "internal",
                error_code=code,
                error_type=type(exc).__name__,
                component="storage" if is_database else "http",
                operation="handle_request",
                stage="request",
                retryable=False,
                frames=safe_frames(exc.__traceback__),
            )
            if not response_started:
                await JSONResponse(
                    {"error": {"code": code, "message": "Не удалось выполнить запрос"}},
                    status_code=503 if is_database else 500,
                )(scope, receive, send_response)
        finally:
            route = scope.get("route")
            self.events.emit(
                "request_completed",
                "HTTP-запрос завершён",
                request_id,
                route=getattr(route, "path", "unmatched"),
                http_status=http_status,
                duration_ms=round((monotonic() - started) * 1000),
            )
