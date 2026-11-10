"""Проверка развёрнутого API и сохранности карточек без вывода ключей."""

import argparse
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

COMPLETE_TEXT = "Нужно передавать заявки из формы WordPress в Битрикс24 и создавать лид."
INCOMPLETE_TEXT = "Нужно что-нибудь с ИИ для менеджеров. Конкретный процесс ещё не выбран."


class VerificationFailure(Exception):
    """Контролируемый результат проверки без внешних сообщений и секретов."""


class NoRedirect(HTTPRedirectHandler):
    """Не передавать ключ на другой адрес при редиректе."""

    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        """Запретить автоматическое перенаправление."""
        return None


def require(condition: bool, message: str) -> None:
    """Остановить проверку, не полагаясь на отключаемый Python assert."""
    if not condition:
        raise VerificationFailure(message)


def load_key(env_file: Path) -> str:
    """Прочитать только ключ приложения; значение не включать в ошибки."""
    key = os.getenv("BRIEF_API_KEY", "")
    if not key and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "BRIEF_API_KEY":
                key = value.strip()
                if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
                    key = key[1:-1]
                break
    require(
        bool(key.strip()) and not key.startswith("replace-with-"),
        "Задайте BRIEF_API_KEY в окружении либо в выбранном .env; ключ в чат не передавайте",
    )
    return key


class ApiClient:
    """HTTP-клиент проверки; HTTPS снаружи, HTTP только на loopback."""

    def __init__(self, base_url: str, key: str) -> None:
        url = urlsplit(base_url)
        require(
            url.scheme == "https"
            or (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")),
            "Нужен HTTPS-адрес приложения либо HTTP на loopback",
        )
        require(
            bool(url.hostname)
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
            and url.path in ("", "/"),
            "Базовый адрес задаётся без пути, query и credentials",
        )
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.opener = build_opener(NoRedirect())

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        operation: str | None = None,
        authenticated: bool = True,
        wrong_key: bool = False,
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        """Получить JSON; не отражать сырые сетевые ошибки или содержимое в stdout."""
        headers = {"Cache-Control": "no-cache"}
        if authenticated:
            headers["X-API-Key"] = secrets.token_urlsafe(32) if wrong_key else self.key
        if operation:
            headers["Idempotency-Key"] = operation
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=90)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read(262145)
            require(len(raw) <= 262144, "Ответ API превысил предел проверки")
            require(self.key.encode() not in raw, "API отразил ключ приложения в ответе")
            parsed = json.loads(raw)
            require(isinstance(parsed, dict), "API вернул JSON не в форме объекта")
            metadata = {name.lower(): value for name, value in response.headers.items()}
            return response.status, parsed, metadata


def get_health(client: ApiClient) -> dict[str, object]:
    """Подтвердить готовность и сохранить версию для сравнения после деплоя."""
    status, health, _ = client.request("GET", "/health", authenticated=False)
    require(status == 200 and health.get("status") == "ok", "Health не подтвердил готовность")
    require(isinstance(health.get("version"), str), "Health не содержит строковую версию")
    return health


def check_card(client: ApiClient, case: dict[str, object]) -> None:
    """Подтвердить чтение и идентичность повтора без создания новой карточки."""
    record = case["record"]
    require(isinstance(record, dict), "Некорректный сохранённый результат проверки")
    status, fetched, _ = client.request("GET", "/requests/" + str(record["id"]))
    require(status == 200 and fetched == record, "Карточка не найдена либо изменилась")
    status, replayed, headers = client.request(
        "POST", "/ingest", payload={"text": case["text"]}, operation=str(case["operation"])
    )
    require(status == 200 and replayed == record, "Повтор не вернул сохранённую карточку")
    require(headers.get("idempotency-replayed") == "true", "Повтор не отмечен в HTTP-заголовке")


def write_state(path: Path, state: dict[str, object]) -> None:
    """Сохранить синтетические карточки и результаты, исключив секреты."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def create_verification(client: ApiClient, path: Path) -> dict[str, object]:
    """Проверить доступ, валидацию, два реальных разбора, чтение и повторы."""
    require(
        not path.exists(),
        "Файл проверки уже существует; используйте --check-existing или новый путь",
    )
    health = get_health(client)
    cases: list[dict[str, object]] = []
    state: dict[str, object] = {
        "base_url": client.base_url,
        "started_at": datetime.now(UTC).isoformat(),
        "version_before": health["version"],
        "cases": cases,
        "checks": {},
    }
    checks: dict[str, str] = {}
    state["checks"] = checks
    operation = "verify-" + uuid4().hex
    for authenticated, wrong in ((False, False), (True, True)):
        status, _, _ = client.request(
            "POST",
            "/ingest",
            payload={"text": COMPLETE_TEXT},
            operation=operation,
            authenticated=authenticated,
            wrong_key=wrong,
        )
        require(status == 401, "API допускает создание без правильного ключа")
    status, _, _ = client.request("GET", "/requests/" + str(uuid4()), authenticated=False)
    require(status == 401, "Чтение карточек не требует ключа")
    status, _, _ = client.request("GET", "/requests/" + str(uuid4()))
    require(status == 404, "Ключ приложения отклонён либо API недоступен; проверьте BRIEF_API_KEY")
    checks["authentication"] = "pass"
    for payload in ({"text": "  "}, {"text": 123}, {"text": "ok", "extra": True}):
        status, _, _ = client.request("POST", "/ingest", payload=payload, operation=operation)
        require(status == 422, "Невалидный ввод не отклонён кодом 422")
    checks["validation"] = "pass"
    checks["missing_card"] = "pass"
    write_state(path, state)

    for name, text, expected_status, expected_escalate in (
        ("complete", COMPLETE_TEXT, "ready_for_review", False),
        ("incomplete", INCOMPLETE_TEXT, "needs_clarification", True),
    ):
        operation = "verify-" + uuid4().hex
        status, record, _ = client.request(
            "POST", "/ingest", payload={"text": text}, operation=operation
        )
        require(status == 201, "Новая заявка не вернула 201")
        case: dict[str, object] = {
            "name": name,
            "text": text,
            "operation": operation,
            "record": record,
        }
        cases.append(case)
        write_state(path, state)
        result = record.get("result")
        require(isinstance(result, dict), "Карточка не содержит результата")
        require(record.get("error_code") is None, "Разбор завершился с ошибкой провайдера")
        require(result.get("status") == expected_status, "Бизнес-статус не совпал с ожиданием")
        require(result.get("escalate") is expected_escalate, "Эскалация не совпала с ожиданием")
        questions = result.get("questions")
        require(isinstance(questions, list) and len(questions) <= 5, "Нарушен формат вопросов")
        check_card(client, case)
        status, conflict, _ = client.request(
            "POST", "/ingest", payload={"text": "Другой текст"}, operation=operation
        )
        error = conflict.get("error")
        require(
            status == 409
            and isinstance(error, dict)
            and error.get("code") == "idempotency_conflict",
            "Повтор с другим текстом не отклонён",
        )
        checks[name + "_ingest_get_replay_conflict"] = "pass"
        write_state(path, state)
        print(f"{name}: POST / GET / повтор / конфликт — PASS", flush=True)
    checks["initial"] = "pass"
    write_state(path, state)
    print(
        "Первичная проверка API: PASS; сохранены две карточки для проверки после обновления",
        flush=True,
    )
    return state


def check_existing(
    client: ApiClient,
    path: Path,
    *,
    require_version_change: bool = False,
    expected_version: str | None = None,
) -> dict[str, object]:
    """Проверить прежние карточки после пересоздания без платных вызовов."""
    state = json.loads(path.read_text(encoding="utf-8"))
    require(state.get("base_url") == client.base_url, "Файл проверки относится к другому API")
    require(state.get("checks", {}).get("initial") == "pass", "Первичная проверка не завершена")
    health = get_health(client)
    if require_version_change:
        require(health["version"] != state["version_before"], "Новая версия ещё не развёрнута")
    if expected_version:
        require(health["version"] == expected_version, "Health не совпал с ожидаемым SHA")
    for case in state["cases"]:
        check_card(client, case)
    state["version_after"] = health["version"]
    state["checked_at"] = datetime.now(UTC).isoformat()
    state["checks"]["persistent_cards_and_replay"] = "pass"
    if require_version_change:
        state["checks"]["version_transition"] = "pass"
    write_state(path, state)
    print("Сохранность карточек и идемпотентность: PASS", flush=True)
    return state


def main() -> int:
    """Запустить проверку; секрет не принимается аргументом командной строки."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Адрес проверяемого приложения")
    parser.add_argument("--state", type=Path, required=True, help="Файл результатов в output/")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="Локальный файл ключа")
    parser.add_argument(
        "--check-existing", action="store_true", help="Проверить ранее созданные карточки"
    )
    parser.add_argument("--require-version-change", action="store_true", help="Требовать новый SHA")
    parser.add_argument("--expected-version", help="Ожидаемая версия после обновления")
    args = parser.parse_args()
    if args.require_version_change and not args.check_existing:
        parser.error("--require-version-change используется с --check-existing")
    try:
        client = ApiClient(args.base_url, load_key(args.env_file))
        if args.check_existing:
            check_existing(
                client,
                args.state,
                require_version_change=args.require_version_change,
                expected_version=args.expected_version,
            )
        else:
            create_verification(client, args.state)
        return 0
    except VerificationFailure as exc:
        print("FAIL: " + str(exc), flush=True)
        return 1
    except (OSError, ValueError):
        # Не печатать сырую сетевую ошибку: она может включать URL или содержимое ответа.
        print(
            "Проверка не завершена: проверьте доступ, конфигурацию и сохранённые результаты",
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
