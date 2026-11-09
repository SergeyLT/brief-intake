"""Запуск одного ресурса Coolify и ограниченное ожидание ожидаемой версии."""

import json
import os
import re
import sys
import time
from collections.abc import Callable
from email.message import Message
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    """Не пересылать токен или health-запросы на неожиданный адрес."""

    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: Message, newurl: str
    ) -> None:
        """Отклонить редирект, сохранив исходную границу доверия."""
        return None


def valid_https_url(value: str) -> bool:
    """Проверить HTTPS без встроенных credentials, пробелов и fragment."""
    try:
        url = urlsplit(value)
        return (
            bool(value)
            and not any(char.isspace() for char in value)
            and url.scheme == "https"
            and bool(url.hostname)
            and url.username is None
            and url.password is None
            and not url.fragment
            and (url.port is None or 1 <= url.port <= 65535)
        )
    except ValueError:
        return False


def is_deploy_webhook(value: str) -> bool:
    """Принять только endpoint одного ресурса, исключив Manual Git Webhook и tag."""
    if not valid_https_url(value):
        return False
    try:
        url = urlsplit(value)
        query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
        identifiers = query.get("uuid", [])
        return (
            url.path == "/api/v1/deploy"
            and set(query) <= {"uuid", "force"}
            and len(identifiers) == 1
            and re.fullmatch(r"[A-Za-z0-9_-]+", identifiers[0]) is not None
            and query.get("force", ["false"]) == ["false"]
        )
    except ValueError:
        return False


def health_url(base_url: str) -> str:
    """Проверить публичный базовый адрес до отправки deploy webhook."""
    if not valid_https_url(base_url):
        raise ValueError("PUBLIC_BASE_URL должен быть публичным HTTPS-адресом приложения")
    url = urlsplit(base_url)
    if url.path not in ("", "/") or url.query:
        raise ValueError("PUBLIC_BASE_URL задаётся без /health, другого пути и query")
    return base_url.rstrip("/") + "/health"


def wait_for_version(
    url: str,
    expected: str,
    fetch: Callable[[str], object],
    *,
    deadline_seconds: float = 300,
    interval: float = 5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Опросить health без вывода внешнего содержимого и без вечного ожидания."""
    deadline = clock() + deadline_seconds
    while clock() < deadline:
        try:
            payload = fetch(url)
            if (
                isinstance(payload, dict)
                and payload.get("status") == "ok"
                and payload.get("version") == expected
            ):
                print("Ожидаемая версия приложения подтверждена", flush=True)
                return True
        except (HTTPError, URLError, OSError, ValueError):
            pass
        remaining = deadline - clock()
        if remaining > 0:
            print("Ожидание готовности новой версии приложения", flush=True)
            sleep(min(interval, remaining))
    return False


def main() -> int:
    """Прочитать секреты только из окружения и не повторять webhook автоматически."""
    webhook = os.getenv("COOLIFY_WEBHOOK", "")
    token = os.getenv("COOLIFY_TOKEN", "")
    version = os.getenv("GITHUB_SHA", "")
    try:
        if not is_deploy_webhook(webhook):
            raise ValueError(
                "Нужен Deploy Webhook (auth required): /api/v1/deploy?uuid=...&force=false"
            )
        if not token.strip():
            raise ValueError("Не задан COOLIFY_TOKEN с правом Deploy")
        if not re.fullmatch(r"[a-f0-9]{40}", version):
            raise ValueError("GITHUB_SHA должен содержать полный SHA коммита")
        url = health_url(os.getenv("PUBLIC_BASE_URL", ""))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    opener = build_opener(NoRedirect())
    request = Request(
        webhook, data=b"", headers={"Authorization": f"Bearer {token}"}, method="POST"
    )
    try:
        with opener.open(request, timeout=20) as response:
            if not 200 <= response.status < 300:
                raise ValueError("Неуспешный HTTP-статус")
        print("Coolify принял запрос деплоя; ожидается новая версия", flush=True)
    except (HTTPError, URLError, OSError, ValueError):
        print(
            "Coolify не подтвердил приём запроса. "
            "Проверьте ресурс и журнал; повтор не выполняется.",
            file=sys.stderr,
        )
        return 1

    def fetch(address: str) -> object:
        request = Request(address, headers={"Cache-Control": "no-cache"})
        with opener.open(request, timeout=5) as response:
            return json.loads(response.read(65536))

    if wait_for_version(url, version, fetch):
        return 0
    print("Новая SHA-версия не стала доступна за отведённое время", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
