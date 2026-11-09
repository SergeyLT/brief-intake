"""Проверки предметной области поверх ответа модели."""

from brief_intake.models import BriefFields, Extraction, Result

FIELD_QUESTIONS = {
    "goal": "Какую задачу или проблему нужно решить автоматизацией?",
    "source": "Откуда поступают данные для обработки?",
    "action": "Какой результат или действие требуется на выходе?",
    "target": "В какую систему нужно передавать данные?",
}


def safe_result(error_code: str) -> Result:
    """Проверяемый шаблон без выдуманных фактов и обещаний действий."""
    return Result(
        scope="unclear",
        process_type="unclear",
        summary="Автоматический разбор не завершён. Требуется проверка специалистом.",
        fields=BriefFields.empty(),
        confidence="low",
        escalate=True,
        conflicts=[],
        missing_information=[],
        questions=[],
        draft_reply="Заявка требует ручного разбора специалистом.",
        out_of_scope_reason=None,
        status="error",
        next_action="manual_review",
        escalation_reasons=[error_code],
    )


def validate_extraction(extraction: Extraction, text: str) -> Result:
    """Проверить цитаты, полноту и обязательную эскалацию независимо от LLM."""
    normalized = " ".join(text.split())
    for name in BriefFields.model_fields:
        fact = getattr(extraction.fields, name)
        if fact is not None and " ".join(fact.evidence.split()) not in normalized:
            raise ValueError("evidence_not_in_source")

    reasons: list[str] = []
    if extraction.confidence == "low":
        reasons.append("low_confidence")
    if extraction.conflicts:
        reasons.append("conflicting_requirements")
    if extraction.escalate:
        reasons.append("model_requires_review")

    if extraction.scope == "out_of_scope" and extraction.confidence != "low":
        if not extraction.out_of_scope_reason:
            raise ValueError("missing_scope_reason")
        return Result(
            **extraction.model_dump(
                exclude={"fields", "questions", "missing_information", "escalate"}
            ),
            fields=BriefFields.empty(),
            questions=[],
            missing_information=[],
            escalate=bool(reasons),
            status="out_of_scope",
            next_action="outside_specialization",
            escalation_reasons=reasons,
            draft_reply=(
                "Спасибо за обращение. Мы занимаемся интеграциями и автоматизацией "
                "обработки данных. " + extraction.out_of_scope_reason
            ),
        )

    required = ["goal", "source", "action"]
    if extraction.process_type == "data_transfer":
        required.append("target")
    missing = [name for name in required if getattr(extraction.fields, name) is None]
    reasons.extend(f"missing_{name}" for name in missing)
    if extraction.scope == "unclear" or extraction.process_type == "unclear":
        reasons.append("unclear_process")
    if extraction.scope == "out_of_scope":
        reasons.append("uncertain_scope")

    # Не добавлять к вопросам модели перефразированные дубли из шаблона.
    questions = list(dict.fromkeys(extraction.questions)) if reasons else []
    if reasons and not questions:
        questions = [FIELD_QUESTIONS[name] for name in missing]
        if not questions:
            questions = ["Уточните, пожалуйста, задачу и какие требования считать окончательными?"]
    information = list(dict.fromkeys(missing + extraction.missing_information))
    return Result(
        **extraction.model_dump(
            exclude={"questions", "missing_information", "escalate", "out_of_scope_reason"}
        ),
        questions=questions[:5],
        missing_information=information,
        out_of_scope_reason=None,
        escalate=bool(reasons),
        status="needs_clarification" if reasons else "ready_for_review",
        next_action="clarify" if reasons else "technical_review",
        escalation_reasons=reasons,
        draft_reply=(
            "Для первичной технической оценки уточните, пожалуйста: " + " ".join(questions[:5])
            if reasons
            else "Понимаем задачу: "
            + extraction.summary
            + " Информации достаточно для первичной технической оценки специалистом."
        ),
    )
