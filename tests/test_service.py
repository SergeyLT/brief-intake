"""Бизнес-правила проверяются независимо от поведения модели."""

import pytest
from conftest import TEXT, extraction

from brief_intake.models import BriefFields, Fact
from brief_intake.service import validate_extraction


def test_complete_brief_is_ready_without_budget_and_plugin() -> None:
    result = validate_extraction(extraction(), TEXT)
    assert result.status == "ready_for_review"
    assert result.escalate is False
    assert result.fields.budget is None


@pytest.mark.parametrize("field", ["goal", "source", "target", "action"])
def test_missing_required_field_overrides_model(field: str) -> None:
    base = extraction()
    fields = base.fields.model_dump()
    fields[field] = None
    result = validate_extraction(extraction(fields=fields), TEXT)
    assert result.status == "needs_clarification"
    assert result.escalate is True
    assert f"missing_{field}" in result.escalation_reasons
    assert 1 <= len(result.questions) <= 5


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"confidence": "low"}, "low_confidence"),
        ({"conflicts": ["Указаны разные целевые системы"]}, "conflicting_requirements"),
        ({"scope": "unclear"}, "unclear_process"),
        ({"escalate": True}, "model_requires_review"),
    ],
)
def test_escalation_cannot_be_disabled(changes: dict, reason: str) -> None:
    result = validate_extraction(extraction(**changes), TEXT)
    assert result.escalate is True
    assert result.status == "needs_clarification"
    assert reason in result.escalation_reasons


def test_unquoted_fact_is_rejected() -> None:
    base = extraction()
    fields = base.fields.model_copy(update={"budget": Fact(value="50000", evidence="50000 рублей")})
    with pytest.raises(ValueError, match="evidence_not_in_source"):
        validate_extraction(extraction(fields=fields.model_dump()), TEXT)


def test_out_of_scope_is_saved_without_crm_questions() -> None:
    value = extraction(
        scope="out_of_scope",
        process_type="unclear",
        fields=BriefFields.empty().model_dump(),
        out_of_scope_reason="Дизайн логотипа не относится к автоматизации",
        questions=["Какая CRM?"],
        missing_information=["CRM"],
    )
    result = validate_extraction(value, "Нужен логотип")
    assert result.status == "out_of_scope"
    assert result.questions == []
    assert result.missing_information == []
    assert all(value is None for value in result.fields.model_dump().values())


def test_uncertain_scope_requires_clarification() -> None:
    value = extraction(
        scope="out_of_scope",
        confidence="low",
        fields=BriefFields.empty().model_dump(),
        out_of_scope_reason="Неясно",
    )
    result = validate_extraction(value, "Нужно что-нибудь с ИИ")
    assert result.status == "needs_clarification"
    assert result.escalate is True


def test_questions_are_not_duplicated_by_fallback_template() -> None:
    base = extraction()
    fields = base.fields.model_dump()
    fields["source"] = None
    question = "Из какой системы должны поступать данные?"
    result = validate_extraction(extraction(fields=fields, questions=[question]), TEXT)
    assert result.questions == [question]
    assert result.draft_reply.count(question) == 1


def test_ready_draft_does_not_require_optional_details() -> None:
    result = validate_extraction(
        extraction(questions=["Какой плагин формы?"], missing_information=["Плагин неизвестен"]),
        TEXT,
    )
    assert result.status == "ready_for_review"
    assert result.questions == []
    assert "достаточно" in result.draft_reply
    assert "плагин" not in result.draft_reply


def test_all_missing_fields_and_model_notes_fit_result() -> None:
    result = validate_extraction(
        extraction(
            fields=BriefFields.empty().model_dump(),
            missing_information=[f"Пробел {i}" for i in range(10)],
        ),
        TEXT,
    )
    assert result.status == "needs_clarification"
    assert len(result.missing_information) == 14
    assert len(result.questions) == 4
    assert len(result.draft_reply) <= 1200
