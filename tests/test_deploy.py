"""Деплой проверяется без обращения к реальному Coolify."""

from urllib.error import URLError
from urllib.request import Request

import pytest

from scripts import deploy


@pytest.mark.parametrize(
    "url",
    [
        "https://coolify.example.test/api/v1/deploy?uuid=resource_123&force=false",
        "https://coolify.example.test/api/v1/deploy?uuid=resource-123",
    ],
)
def test_single_resource_deploy_webhook(url: str) -> None:
    assert deploy.is_deploy_webhook(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://coolify.example.test/api/v1/deploy?uuid=one",
        "https://user:secret@coolify.example.test/api/v1/deploy?uuid=one",
        "https://coolify.example.test/api/v1/deploy?uuid=one,two",
        "https://coolify.example.test/api/v1/deploy?uuid=one&uuid=two",
        "https://coolify.example.test/api/v1/deploy?tag=production",
        "https://coolify.example.test/api/v1/deploy?uuid=one&force=false&tag=all",
        "https://coolify.example.test/api/v1/deploy?uuid=one&force=true",
        "https://coolify.example.test/api/v1/deploy?uuid=one#fragment",
        "https://coolify.example.test/api/v1/deploy?uuid=",
        "https://coolify.example.test/api/v1/webhooks/deploy?uuid=one",
        " https://coolify.example.test/api/v1/deploy?uuid=one",
    ],
)
def test_wrong_webhook_never_receives_token(url: str) -> None:
    assert not deploy.is_deploy_webhook(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://app.example.test",
        "https://app.example.test/health",
        "https://app.example.test?secret=private",
        "https://user:pass@app.example.test",
    ],
)
def test_wrong_health_base_is_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        deploy.health_url(url)


def test_version_polling_requires_expected_sha() -> None:
    now = [0.0]
    answers = iter(
        [
            URLError("PRIVATE"),
            {"status": "ok", "version": "old"},
            {"status": "ok", "version": "a" * 40},
        ]
    )

    def fetch(url: str) -> object:
        result = next(answers)
        if isinstance(result, Exception):
            raise result
        return result

    def sleep(seconds: float) -> None:
        now[0] += seconds

    assert deploy.wait_for_version(
        "https://app.example.test/health",
        "a" * 40,
        fetch,
        clock=lambda: now[0],
        sleep=sleep,
        interval=1,
        deadline_seconds=5,
    )
    assert now[0] == 2


def test_version_polling_stops_on_deadline(capsys) -> None:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    assert not deploy.wait_for_version(
        "https://app.example.test/health",
        "a" * 40,
        lambda url: {"status": "ok", "version": "PRIVATE"},
        clock=lambda: now[0],
        sleep=sleep,
        interval=1,
        deadline_seconds=3,
    )
    assert now[0] == 3
    assert "PRIVATE" not in capsys.readouterr().out


def test_invalid_configuration_does_not_contact_coolify(monkeypatch, capsys) -> None:
    monkeypatch.setenv("COOLIFY_WEBHOOK", "https://bad.example.test?token=PRIVATE")
    monkeypatch.setenv("COOLIFY_TOKEN", "PRIVATE")

    def forbidden(*args: object) -> None:
        pytest.fail("Сетевой клиент не должен создаваться")

    monkeypatch.setattr(deploy, "build_opener", forbidden)
    assert deploy.main() == 1
    assert "PRIVATE" not in capsys.readouterr().err


def test_redirect_is_not_followed() -> None:
    from email.message import Message

    assert (
        deploy.NoRedirect().redirect_request(
            Request("https://coolify.example.test"),
            None,
            302,
            "redirect",
            Message(),
            "https://unexpected.example.test",
        )
        is None
    )


@pytest.mark.parametrize("accepted", [True, False])
def test_webhook_is_sent_once_and_token_is_not_used_for_health(
    monkeypatch, capsys, accepted: bool
) -> None:
    monkeypatch.setenv("COOLIFY_WEBHOOK", "https://coolify.example.test/api/v1/deploy?uuid=one")
    monkeypatch.setenv("COOLIFY_TOKEN", "PRIVATE")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example.test")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit: int) -> bytes:
            return ('{"status":"ok","version":"' + "a" * 40 + '"}').encode()

    class Opener:
        def open(self, request: Request, timeout: float) -> Response:
            requests.append(request)
            if request.get_method() == "POST" and not accepted:
                raise URLError("PRIVATE")
            return Response()

    monkeypatch.setattr(deploy, "build_opener", lambda *args: Opener())
    assert deploy.main() == (0 if accepted else 1)
    assert sum(request.get_method() == "POST" for request in requests) == 1
    if accepted:
        assert requests[0].get_header("Authorization") == "Bearer PRIVATE"
        assert requests[1].get_header("Authorization") is None
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err
