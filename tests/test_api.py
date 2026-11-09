"""HTTP-контракт и сквозной процесс с изолированной моделью."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from conftest import HEADERS, TEXT, FakeProvider, extraction
from fastapi.testclient import TestClient

from brief_intake.app import create_app
from brief_intake.config import Settings
from brief_intake.provider import ProviderFailure, ProviderResult
from brief_intake.storage import Store


def test_ingest_read_replay_and_conflict(settings: Settings, provider: FakeProvider) -> None:
    with TestClient(create_app(settings, provider)) as client:
        first = client.post("/ingest", json={"text": TEXT}, headers=HEADERS)
        assert first.status_code == 201, first.text
        card = first.json()
        assert card["result"]["status"] == "ready_for_review"
        assert card["text"] == TEXT
        assert card["usage"]["total_tokens"] == 150
        assert first.headers["idempotency-replayed"] == "false"
        assert client.get(first.headers["location"], headers=HEADERS).json() == card
        replay = client.post("/ingest", json={"text": TEXT}, headers=HEADERS)
        assert replay.status_code == 200
        assert replay.json() == card
        assert replay.headers["idempotency-replayed"] == "true"
        conflict = client.post("/ingest", json={"text": "Другая заявка"}, headers=HEADERS)
        assert conflict.status_code == 409
        assert provider.calls == 1
        assert client.get("/health").json()["status"] == "ok"
    assert provider.closed

    restarted = FakeProvider()
    with TestClient(create_app(settings, restarted)) as client:
        assert client.post("/ingest", json={"text": TEXT}, headers=HEADERS).json() == card
        assert restarted.calls == 0


@pytest.mark.parametrize(
    "payload", [{"text": ""}, {"text": "   "}, {"text": 123}, {"text": "ok", "extra": 1}, {}]
)
def test_invalid_input_does_not_call_provider(
    settings: Settings, provider: FakeProvider, payload: dict
) -> None:
    with TestClient(create_app(settings, provider)) as client:
        assert client.post("/ingest", json=payload, headers=HEADERS).status_code == 422
        assert provider.calls == 0
        assert Store(settings.database_path).audit() == []


def test_auth_headers_and_invalid_json_do_not_leak(
    settings: Settings, provider: FakeProvider
) -> None:
    with TestClient(create_app(settings, provider)) as client:
        assert client.post("/ingest", json={"text": TEXT}).status_code == 401
        assert client.get("/requests/00000000-0000-0000-0000-000000000000").status_code == 401
        for headers in ({"X-API-Key": settings.api_key}, {**HEADERS, "Idempotency-Key": "bad key"}):
            assert client.post("/ingest", json={"text": TEXT}, headers=headers).status_code == 422
        invalid = client.post(
            "/ingest",
            content='{"text": "PRIVATE',
            headers={**HEADERS, "Content-Type": "application/json"},
        )
        assert invalid.status_code == 422
        assert "PRIVATE" not in invalid.text
        assert settings.api_key not in invalid.text
        assert provider.calls == 0


def test_input_boundary_and_large_http_body(settings: Settings, provider: FakeProvider) -> None:
    config = replace(settings, max_input_chars=len(TEXT))
    with TestClient(create_app(config, provider)) as client:
        assert client.post("/ingest", json={"text": TEXT + " "}, headers=HEADERS).status_code == 422
        assert client.post("/ingest", json={"text": TEXT}, headers=HEADERS).status_code == 201
        huge = client.post("/ingest", content=b"x" * 10000, headers=HEADERS)
        assert huge.status_code == 413
        assert provider.calls == 1


def test_quota_allows_replay_but_not_new_call(settings: Settings, provider: FakeProvider) -> None:
    with TestClient(create_app(replace(settings, daily_call_limit=1), provider)) as client:
        first = client.post("/ingest", json={"text": TEXT}, headers=HEADERS)
        assert first.status_code == 201
        denied = client.post(
            "/ingest", json={"text": TEXT}, headers={**HEADERS, "Idempotency-Key": "new"}
        )
        assert denied.status_code == 429
        assert int(denied.headers["retry-after"]) > 0
        assert client.post("/ingest", json={"text": TEXT}, headers=HEADERS).status_code == 200
        assert provider.calls == 1


@pytest.mark.parametrize(
    "failure, code",
    [
        (ProviderFailure("provider_timeout", "APITimeoutError"), "provider_timeout"),
        (RuntimeError("PRIVATE test-provider-key"), "unexpected_processing_error"),
    ],
)
def test_failure_is_saved_and_not_retried(
    settings: Settings, failure: Exception, code: str, capsys
) -> None:
    provider = FakeProvider(failure)
    with TestClient(create_app(settings, provider)) as client:
        first = client.post("/ingest", json={"text": TEXT}, headers=HEADERS)
        assert first.status_code == 201
        card = first.json()
        assert card["result"]["status"] == "error"
        assert card["result"]["escalate"] is True
        assert card["error_code"] == code
        assert client.post("/ingest", json={"text": TEXT}, headers=HEADERS).json() == card
        assert provider.calls == 1
    logs = capsys.readouterr().out
    for sensitive in (TEXT, "PRIVATE", settings.api_key, settings.proxyapi_key):
        assert sensitive not in logs
    events = [json.loads(line) for line in logs.splitlines()]
    assert all(event["request_id"] for event in events)
    assert any(event.get("error_code") == code for event in events)


def test_storage_failure_does_not_claim_success(
    settings: Settings, provider: FakeProvider, monkeypatch
) -> None:
    with TestClient(create_app(settings, provider)) as client:

        def fail(*args: object, **kwargs: object) -> None:
            raise sqlite3.OperationalError("PRIVATE test-provider-key")

        monkeypatch.setattr(Store, "complete", fail)
        response = client.post("/ingest", json={"text": TEXT}, headers=HEADERS)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "storage_unavailable"
        assert "PRIVATE" not in response.text


def test_simultaneous_posts_call_model_once(settings: Settings) -> None:
    entered, release = Event(), Event()

    class BlockingProvider(FakeProvider):
        def extract(self, text: str) -> ProviderResult:
            self.calls += 1
            entered.set()
            assert release.wait(5)
            return ProviderResult(extraction(), None)

    provider = BlockingProvider()
    with (
        TestClient(create_app(settings, provider)) as client,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        future = pool.submit(client.post, "/ingest", json={"text": TEXT}, headers=HEADERS)
        try:
            assert entered.wait(5)
            duplicate = client.post("/ingest", json={"text": TEXT}, headers=HEADERS)
            assert duplicate.status_code == 409
            assert duplicate.json()["error"]["code"] == "operation_processing"
        finally:
            release.set()
        assert future.result().status_code == 201
        assert provider.calls == 1
