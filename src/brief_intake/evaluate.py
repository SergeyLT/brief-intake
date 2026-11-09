"""Ручной прогон синтетических заявок через API, расходующий квоту и токены."""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import Field, TypeAdapter

from brief_intake.models import BusinessStatus, RequestRecord, StrictModel


class EvalCase(StrictModel):
    """Ожидания фиксируются до вызова модели и не меняются под ответы."""

    id: str
    text: str
    expected_status: BusinessStatus
    expected_escalate: bool
    null_fields: list[str]
    notes: str


class Evaluation(StrictModel):
    """Накопленные результаты прогона, включая сбои API."""

    started_at: str
    cases: list[dict[str, object]] = Field(default_factory=list)


def assess(case: EvalCase, record: RequestRecord) -> list[str]:
    """Автоматические проверки не заменяют ручную оценку смысла черновика."""
    result = record.result
    if result is None:
        return ["Обработка не завершена"]
    errors = []
    if result.status != case.expected_status:
        errors.append(f"Статус {result.status}, ожидался {case.expected_status}")
    if result.escalate != case.expected_escalate:
        errors.append("Эскалация отличается от ожидания")
    for name in case.null_fields:
        if getattr(result.fields, name, None) is not None:
            errors.append(f"Поле {name} должно быть null")
    if case.id == "B07" and not result.conflicts:
        errors.append("Не обнаружено противоречие")
    if case.id == "B08" and (result.questions or not result.out_of_scope_reason):
        errors.append("Для нецелевой заявки нужны объяснение и отсутствие вопросов")
    if case.id == "B09" and (
        result.fields.deadline is None or "конца месяца" not in result.fields.deadline.value
    ):
        errors.append("Относительный срок изменён или потерян")
    if record.error_code is not None:
        errors.append(f"Ошибка обработки: {record.error_code}")
    return errors


def main() -> None:
    """Отправить входы и сохранить артефакт в output для ручной проверки."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Адрес приложения")
    parser.add_argument(
        "--cases", type=Path, default=Path("evals/cases.json"), help="Файл ожиданий"
    )
    parser.add_argument(
        "--output", type=Path, default=Path("output/evaluation.json"), help="Результаты"
    )
    args = parser.parse_args()
    api_key = os.getenv("BRIEF_API_KEY", "")
    if not api_key:
        parser.error("Задайте BRIEF_API_KEY в окружении команды; ключ не передаётся аргументом")
    cases = TypeAdapter(list[EvalCase]).validate_json(args.cases.read_text(encoding="utf-8"))
    report = Evaluation(started_at=datetime.now(UTC).isoformat())
    run_id = str(uuid4())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    failed = 0
    for case in cases:
        request = Request(
            args.base_url.rstrip("/") + "/ingest",
            data=json.dumps({"text": case.text}, ensure_ascii=False).encode(),
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
                "Idempotency-Key": f"eval-{run_id}-{case.id}",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=90) as response:
                record = RequestRecord.model_validate_json(response.read())
            errors = assess(case, record)
            item: dict[str, object] = {
                "id": case.id,
                "errors": errors,
                "record": record.model_dump(),
            }
        except (HTTPError, URLError, TimeoutError) as exc:
            errors = [f"Ошибка API: {type(exc).__name__}"]
            item = {"id": case.id, "errors": errors}
        failed += bool(errors)
        report.cases.append(item)
        args.output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        print(f"{case.id}: {'FAIL' if errors else 'OK'}")
    print(f"Проверено: {len(cases)}, с расхождениями: {failed}. Нужна ручная оценка черновиков.")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
