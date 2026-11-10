"""Проверка процедуры smoke-теста на настоящем HTTP-контракте приложения."""

from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FakeProvider, extraction
from fastapi.testclient import TestClient

from brief_intake.app import create_app
from brief_intake.config import Settings
from brief_intake.models import BriefFields
from brief_intake.provider import ProviderResult
from scripts.verify_api import (
    ApiClient,
    VerificationFailure,
    check_existing,
    create_verification,
    load_key,
)


class ScenarioProvider(FakeProvider):
    """Два контролируемых результата без вызова реальной модели."""

    def extract(self, text: str) -> ProviderResult:
        self.calls += 1
        if "Конкретный процесс" in text:
            return ProviderResult(
                extraction(
                    fields=BriefFields.empty().model_dump(),
                    scope="unclear",
                    process_type="unclear",
                    confidence="low",
                    escalate=True,
                ),
                None,
            )
        return ProviderResult(extraction(), None)


class LocalClient(ApiClient):
    """Выполнить запросы verifier через FastAPI TestClient."""

    def __init__(self, http: TestClient, key: str) -> None:
        super().__init__("http://127.0.0.1:8000", key)
        self.http = http

    def request(
        self,
        method: str,
        path: str,
        *,
        payload=None,
        operation=None,
        authenticated=True,
        wrong_key=False,
    ):
        headers = {}
        if authenticated:
            headers["X-API-Key"] = "wrong-key" if wrong_key else self.key
        if operation:
            headers["Idempotency-Key"] = operation
        response = self.http.request(method, path, json=payload, headers=headers)
        return response.status_code, response.json(), dict(response.headers)


def test_smoke_and_saved_cards_after_version_change(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    first = ScenarioProvider()
    with TestClient(create_app(settings, first)) as http:
        client = LocalClient(http, settings.api_key)
        state = create_verification(client, path)
        assert state["checks"]["initial"] == "pass"
        assert first.calls == 2
        with pytest.raises(VerificationFailure, match="уже существует"):
            create_verification(client, path)
        with pytest.raises(VerificationFailure, match="ещё не развёрнута"):
            check_existing(client, path, require_version_change=True)
        assert first.calls == 2

    second = ScenarioProvider()
    with TestClient(create_app(replace(settings, version="next-version"), second)) as http:
        state = check_existing(
            LocalClient(http, settings.api_key),
            path,
            require_version_change=True,
            expected_version="next-version",
        )
        assert state["checks"]["version_transition"] == "pass"
        assert second.calls == 0
    content = path.read_text(encoding="utf-8")
    assert settings.api_key not in content
    assert settings.proxyapi_key not in content


def test_local_env_key_is_read_without_printing(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("BRIEF_API_KEY", raising=False)
    path = tmp_path / ".env"
    path.write_text('# комментарий\nBRIEF_API_KEY="private-value"\n', encoding="utf-8")
    assert load_key(path) == "private-value"
    monkeypatch.setenv("BRIEF_API_KEY", "environment-value")
    assert load_key(path) == "environment-value"
    captured = capsys.readouterr()
    assert "private-value" not in captured.out + captured.err


def test_wrong_production_key_fails_before_model_calls(settings: Settings, tmp_path: Path) -> None:
    provider = ScenarioProvider()
    with TestClient(create_app(settings, provider)) as http:
        with pytest.raises(VerificationFailure, match="Ключ приложения отклонён"):
            create_verification(LocalClient(http, "wrong-key"), tmp_path / "state.json")
    assert provider.calls == 0


@pytest.mark.parametrize(
    "url",
    [
        "http://external.example",
        "https://user:secret@example.com",
        "https://example.com/health",
        "https://example.com?secret=value",
    ],
)
def test_verifier_rejects_unsafe_base_address(url: str) -> None:
    with pytest.raises(VerificationFailure):
        ApiClient(url, "test-key")
