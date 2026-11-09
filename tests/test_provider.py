"""Реальный SDK работает с подменённым HTTP-транспортом, без платных вызовов."""

import json

import httpx
import pytest
from conftest import TEXT, extraction
from openai import OpenAI

from brief_intake.config import Settings
from brief_intake.provider import ProviderFailure, ProxyAPIProvider


def response_body(content: str | None, finish: str = "stop", refusal: str | None = None) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "openai/gpt-6-luna",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
    }


def test_wire_request_and_usage(settings: Settings) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url) == "https://api.proxyapi.ru/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "openai/gpt-6-luna"
        assert payload["reasoning_effort"] == "none"
        assert payload["temperature"] == 0.2
        assert payload["max_completion_tokens"] == 2000
        schema = payload["response_format"]["json_schema"]
        assert schema["strict"] is True
        assert schema["schema"]["additionalProperties"] is False
        assert set(schema["schema"]["properties"]) == set(schema["schema"]["required"])
        return httpx.Response(200, json=response_body(extraction().model_dump_json()))

    client = OpenAI(
        api_key=settings.proxyapi_key,
        base_url=settings.base_url,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    provider = ProxyAPIProvider(settings, client)
    try:
        result = provider.extract(TEXT)
        assert result.extraction.fields.source is not None
        assert result.usage is not None and result.usage.total_tokens == 200
        assert len(calls) == 1
    finally:
        provider.close()


@pytest.mark.parametrize(
    "content, finish, refusal, code",
    [
        ("not json", "stop", None, "provider_invalid_response"),
        ('{"summary":"incomplete"}', "stop", None, "provider_invalid_response"),
        (None, "stop", "Отказ", "provider_incomplete_response"),
        ("{}", "length", None, "provider_incomplete_response"),
    ],
)
def test_invalid_responses(
    settings: Settings, content: str | None, finish: str, refusal: str | None, code: str
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=response_body(content, finish, refusal))
    )
    client = OpenAI(
        api_key=settings.proxyapi_key,
        base_url=settings.base_url,
        max_retries=0,
        http_client=httpx.Client(transport=transport),
    )
    provider = ProxyAPIProvider(settings, client)
    try:
        with pytest.raises(ProviderFailure) as caught:
            provider.extract(TEXT)
        assert caught.value.code == code
        assert caught.value.usage is not None
        assert caught.value.usage.total_tokens == 200
    finally:
        provider.close()


@pytest.mark.parametrize(
    "failure, expected",
    [
        ("timeout", "provider_timeout"),
        ("429", "provider_rate_limited"),
        ("500", "provider_http_error"),
    ],
)
def test_http_failure_is_not_retried(settings: Settings, failure: str, expected: str) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("PRIVATE", request=request)
        return httpx.Response(
            int(failure), json={"error": {"message": "PRIVATE", "type": "server"}}
        )

    client = OpenAI(
        api_key=settings.proxyapi_key,
        base_url=settings.base_url,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    provider = ProxyAPIProvider(settings, client)
    try:
        with pytest.raises(ProviderFailure) as caught:
            provider.extract(TEXT)
        assert caught.value.code == expected
        assert "PRIVATE" not in str(caught.value)
        assert len(calls) == 1
    finally:
        provider.close()
