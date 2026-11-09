"""Конфигурация из окружения без чтения секретов при импорте."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Настройки одного экземпляра приложения."""

    api_key: str = field(repr=False)
    proxyapi_key: str = field(repr=False)
    database_path: Path = Path("output/briefs.sqlite3")
    base_url: str = "https://api.proxyapi.ru/v1"
    model: str = "openai/gpt-6-luna"
    timeout_seconds: float = 45
    max_completion_tokens: int = 2000
    temperature: float = 0.2
    max_input_chars: int = 10000
    daily_call_limit: int = 100
    environment: str = "local"
    version: str = "0.1.0"

    def __post_init__(self) -> None:
        """Отклонить пустые ключи и опасные значения настроек."""
        placeholders = ("replace-with-", "your-")
        for name, value in (
            ("BRIEF_API_KEY", self.api_key),
            ("PROXYAPI_API_KEY", self.proxyapi_key),
        ):
            if not value.strip() or value.startswith(placeholders):
                raise ValueError(f"Задайте {name}; значение из примера использовать нельзя")
        if not self.base_url.startswith("https://"):
            raise ValueError("PROXYAPI_BASE_URL должен использовать HTTPS")
        if not all(
            value > 0
            for value in (self.max_input_chars, self.daily_call_limit, self.max_completion_tokens)
        ):
            raise ValueError("Ограничения длины и числа вызовов должны быть положительными")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("PROXYAPI_TIMEOUT_SECONDS должен быть положительным числом")
        if not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2:
            raise ValueError("PROXYAPI_TEMPERATURE должна быть в диапазоне 0–2")

    @classmethod
    def from_env(cls) -> "Settings":
        """Прочитать настройки; .env передаётся Docker Compose или загрузчиком uvicorn."""
        try:
            return cls(
                api_key=os.getenv("BRIEF_API_KEY", ""),
                proxyapi_key=os.getenv("PROXYAPI_API_KEY", ""),
                database_path=Path(os.getenv("DATABASE_PATH", "output/briefs.sqlite3")),
                base_url=os.getenv("PROXYAPI_BASE_URL", "https://api.proxyapi.ru/v1"),
                model=os.getenv("PROXYAPI_MODEL", "openai/gpt-6-luna"),
                timeout_seconds=float(os.getenv("PROXYAPI_TIMEOUT_SECONDS", "45")),
                max_completion_tokens=int(os.getenv("PROXYAPI_MAX_COMPLETION_TOKENS", "2000")),
                temperature=float(os.getenv("PROXYAPI_TEMPERATURE", "0.2")),
                max_input_chars=int(os.getenv("MAX_INPUT_CHARS", "10000")),
                daily_call_limit=int(os.getenv("DAILY_LLM_CALL_LIMIT", "100")),
                environment=os.getenv("APP_ENVIRONMENT", "local"),
                version=os.getenv("APP_VERSION", "0.1.0"),
            )
        except (ValueError, OverflowError):
            # Не включать исходное исключение: оно может содержать значение переменной.
            raise ValueError(
                "Некорректная конфигурация: проверьте ключи и числовые настройки"
            ) from None
