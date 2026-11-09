"""Строгие контракты модели, результата и сохранённой карточки."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
QuestionText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
Confidence = Literal["high", "medium", "low"]
BusinessStatus = Literal["ready_for_review", "needs_clarification", "out_of_scope", "error"]


class StrictModel(BaseModel):
    """Запретить дополнительные поля и неявное преобразование типов."""

    model_config = ConfigDict(extra="forbid", strict=True)


class IngestRequest(StrictModel):
    """Исходный текст сохраняется без обрезания и нормализации."""

    text: str = Field(min_length=1)


class Fact(StrictModel):
    """Извлечённое значение и дословный подтверждающий фрагмент."""

    value: ShortText
    evidence: ShortText


class BriefFields(StrictModel):
    """Отсутствующие сведения обозначаются null, а не догадкой."""

    goal: Fact | None
    current_process: Fact | None
    source: Fact | None
    target: Fact | None
    action: Fact | None
    rules: Fact | None
    volume: Fact | None
    deadline: Fact | None
    budget: Fact | None

    @classmethod
    def empty(cls) -> "BriefFields":
        """Пустой набор для безопасного результата при сбое."""
        return cls(**dict.fromkeys(cls.model_fields))


class Extraction(StrictModel):
    """Схема JSON, которую получает провайдер LLM."""

    scope: Literal["in_scope", "out_of_scope", "unclear"]
    process_type: Literal["data_transfer", "other_automation", "unclear"]
    summary: ShortText
    fields: BriefFields
    confidence: Confidence
    escalate: bool
    conflicts: list[ShortText] = Field(max_length=5)
    missing_information: list[ShortText] = Field(max_length=10)
    questions: list[QuestionText] = Field(max_length=5)
    out_of_scope_reason: ShortText | None


class Result(Extraction):
    """Статус и следующий шаг определяются приложением."""

    status: BusinessStatus
    next_action: Literal["technical_review", "clarify", "manual_review", "outside_specialization"]
    escalation_reasons: list[str]
    missing_information: list[ShortText] = Field(max_length=14)
    draft_reply: str = Field(max_length=1200)


class TokenUsage(StrictModel):
    """Измеренный расход; отсутствие usage не означает нулевую стоимость."""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class RequestRecord(StrictModel):
    """Публичное представление записи аудита; ключ операции не возвращается."""

    id: str
    created_at: str
    updated_at: str
    processing_state: Literal["processing", "completed", "interrupted"]
    text: str
    result: Result | None
    error_code: str | None
    error_type: str | None
    model: str
    usage: TokenUsage | None
