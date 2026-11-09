"""Изолированные настройки и проверяемая подмена провайдера."""

from pathlib import Path

import pytest

from brief_intake.config import Settings
from brief_intake.models import BriefFields, Extraction, Fact, TokenUsage
from brief_intake.provider import ProviderResult

TEXT = "Нужно передавать заявки из формы WordPress в Битрикс24 и создавать лид."
HEADERS = {"X-API-Key": "test-app-key", "Idempotency-Key": "test-operation"}


def extraction(**changes: object) -> Extraction:
    """Заявка с проверяемыми цитатами без сетевого вызова."""
    fields = BriefFields.empty().model_copy(
        update={
            "goal": Fact(value="Передача заявок", evidence="передавать заявки"),
            "source": Fact(value="Форма WordPress", evidence="формы WordPress"),
            "target": Fact(value="Битрикс24", evidence="Битрикс24"),
            "action": Fact(value="Создать лид", evidence="создавать лид"),
        }
    )
    value = Extraction(
        scope="in_scope",
        process_type="data_transfer",
        summary="Передача заявок в CRM",
        fields=fields,
        confidence="high",
        escalate=False,
        conflicts=[],
        missing_information=[],
        questions=[],
        out_of_scope_reason=None,
    )
    return Extraction.model_validate({**value.model_dump(), **changes})


class FakeProvider:
    """Считает вызовы и возвращает заданное извлечение либо ошибку."""

    def __init__(self, value: Extraction | Exception | None = None) -> None:
        self.value = value if value is not None else extraction()
        self.calls = 0
        self.closed = False

    def extract(self, text: str) -> ProviderResult:
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return ProviderResult(
            self.value, TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150)
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Каждый тест получает отдельную SQLite внутри проектного basetemp."""
    return Settings(
        api_key="test-app-key",
        proxyapi_key="test-provider-key",
        database_path=tmp_path / "briefs.sqlite3",
    )


@pytest.fixture
def provider() -> FakeProvider:
    """Обычный успешный провайдер."""
    return FakeProvider()
