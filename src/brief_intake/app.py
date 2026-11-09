"""HTTP API первичного разбора брифов."""

import logging
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from brief_intake.config import Settings
from brief_intake.logging_config import EventLogger, safe_frames
from brief_intake.middleware import RequestGuard
from brief_intake.models import IngestRequest, RequestRecord
from brief_intake.provider import Provider, ProviderFailure, ProxyAPIProvider
from brief_intake.service import safe_result, validate_extraction
from brief_intake.storage import DatabaseLock, OperationConflict, QuotaExceeded, Store


def create_app(settings: Settings | None = None, provider: Provider | None = None) -> FastAPI:
    """Создать независимый экземпляр; окружение читается только при запуске фабрики."""
    config = settings or Settings.from_env()
    store = Store(config.database_path.resolve())
    lock = DatabaseLock(store.path)
    events = EventLogger(config)
    model_provider: Provider = provider or ProxyAPIProvider(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        lock.acquire()
        try:
            store.initialize()
            yield
        finally:
            model_provider.close()
            lock.release()

    app = FastAPI(
        title="Brief Intake",
        description="Первичный разбор заявок на интеграции и автоматизацию",
        version=config.version,
        lifespan=lifespan,
    )
    app.add_middleware(
        RequestGuard, events=events, max_body_bytes=config.max_input_chars * 12 + 4096
    )
    security = APIKeyHeader(name="X-API-Key", auto_error=False)

    def authorize(key: Annotated[str | None, Depends(security)]) -> None:
        if key is None or not secrets.compare_digest(key.encode(), config.api_key.encode()):
            raise HTTPException(status_code=401, detail="Отсутствует или неверен ключ приложения")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # exc.errors() содержит input; не отражаем пользовательский ввод и заголовки.
        return JSONResponse(
            {
                "error": {
                    "code": "request_validation_failed",
                    "message": "Проверьте JSON, поля и типы по схеме API",
                }
            },
            status_code=422,
        )

    @app.get("/health", summary="Готовность приложения")
    def health() -> dict[str, str]:
        store.check()
        return {"status": "ok", "version": config.version}

    @app.post(
        "/ingest",
        response_model=RequestRecord,
        status_code=201,
        dependencies=[Depends(authorize)],
        summary="Разобрать заявку и сохранить карточку",
    )
    def ingest(
        body: IngestRequest,
        request: Request,
        response: Response,
        operation_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> RequestRecord | JSONResponse:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", operation_key):
            raise HTTPException(422, "Idempotency-Key: допустимы латинские буквы, цифры, _, . и -")
        if not body.text.strip() or len(body.text) > config.max_input_chars:
            raise HTTPException(
                422,
                f"Текст должен содержать от 1 до {config.max_input_chars} символов "
                "и не быть пробельным",
            )
        try:
            reservation = store.reserve(
                operation_key, body.text, config.model, config.daily_call_limit
            )
        except OperationConflict as exc:
            return JSONResponse(
                {
                    "error": {
                        "code": exc.code,
                        "message": "Идентификатор занят другой или незавершённой обработкой",
                        "record_id": exc.record_id,
                    }
                },
                status_code=409,
            )
        except QuotaExceeded:
            now = datetime.now(UTC)
            midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return JSONResponse(
                {
                    "error": {
                        "code": "daily_call_limit",
                        "message": "Суточный лимит вызовов модели исчерпан",
                    }
                },
                status_code=429,
                headers={"Retry-After": str(max(1, int((midnight - now).total_seconds())))},
            )
        response.headers["Location"] = f"/requests/{reservation.record.id}"
        response.headers["Idempotency-Replayed"] = str(reservation.replayed).lower()
        if reservation.replayed:
            response.status_code = 200
            return reservation.record

        usage = None
        error_code = None
        error_type = None
        try:
            extracted = model_provider.extract(body.text)
            usage = extracted.usage
            try:
                result = validate_extraction(extracted.extraction, body.text)
            except ValueError:
                raise ProviderFailure(
                    "provider_invalid_evidence", "EvidenceValidationError", usage
                ) from None
        except ProviderFailure as exc:
            error_code, error_type, usage = exc.code, exc.error_type, exc.usage
            result = safe_result(error_code)
            events.emit(
                "provider_request_failed",
                "Модель не дала пригодный результат",
                request.state.request_id,
                logging.WARNING,
                operation_id=reservation.record.id,
                error_category="timeout"
                if error_code == "provider_timeout"
                else "external_service",
                error_code=error_code,
                error_type=error_type,
                component="llm_client",
                operation="extract_brief",
                stage="provider_request",
                retryable=False,
                retries_exhausted=True,
            )
        except Exception as exc:
            error_code, error_type = "unexpected_processing_error", type(exc).__name__
            result = safe_result(error_code)
            events.emit(
                "processing_failed",
                "Автоматический разбор завершился с ошибкой",
                request.state.request_id,
                logging.ERROR,
                operation_id=reservation.record.id,
                error_category="internal",
                error_code=error_code,
                error_type=error_type,
                component="processing",
                operation="extract_brief",
                stage="validation",
                retryable=False,
                frames=safe_frames(exc.__traceback__),
            )
        record = store.complete(reservation.record.id, result, usage, error_code, error_type)
        events.emit(
            "brief_processed",
            "Карточка заявки сохранена",
            request.state.request_id,
            operation_id=record.id,
            status=result.status,
            replayed=False,
        )
        return record

    @app.get(
        "/requests/{record_id}",
        response_model=RequestRecord,
        dependencies=[Depends(authorize)],
        summary="Получить карточку и аудит",
    )
    def get_request(record_id: UUID) -> RequestRecord:
        record = store.get(str(record_id))
        if record is None:
            raise HTTPException(404, "Карточка не найдена")
        return record

    return app
