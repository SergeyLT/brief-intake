"""Настраиваемость ограничений и безопасность диагностического вывода."""

import json
from dataclasses import replace

import pytest

from brief_intake.config import Settings
from brief_intake.logging_config import EventLogger


def test_env_overrides_defaults(monkeypatch) -> None:
    monkeypatch.setenv("BRIEF_API_KEY", "test-app-key")
    monkeypatch.setenv("PROXYAPI_API_KEY", "test-provider-key")
    monkeypatch.setenv("MAX_INPUT_CHARS", "5000")
    monkeypatch.setenv("DAILY_LLM_CALL_LIMIT", "7")
    settings = Settings.from_env()
    assert settings.max_input_chars == 5000
    assert settings.daily_call_limit == 7
    assert "test-provider-key" not in repr(settings)


@pytest.mark.parametrize(
    "changes",
    [
        {"daily_call_limit": 0},
        {"max_input_chars": -1},
        {"api_key": "replace-with-random-application-key"},
        {"timeout_seconds": float("nan")},
        {"temperature": 3},
    ],
)
def test_bad_settings_fail_early(settings: Settings, changes: dict) -> None:
    with pytest.raises(ValueError):
        replace(settings, **changes)


def test_log_whitelist_masking_and_no_duplicate_handlers(settings: Settings, capsys) -> None:
    first = EventLogger(settings)
    second = EventLogger(settings)
    first.emit(
        "test",
        "Тест",
        "server-id",
        text="PRIVATE",
        authorization="SECRET",
        error_code=settings.proxyapi_key,
    )
    second.emit("test", "Тест", "server-id")
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    event = json.loads(lines[0])
    assert event["error_code"] == "[СКРЫТО]"
    assert "text" not in event and "authorization" not in event
    assert {"timestamp", "level", "service", "version", "environment", "request_id"} <= event.keys()
