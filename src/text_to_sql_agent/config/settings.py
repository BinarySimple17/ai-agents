"""Единственная точка чтения окружения (S00 правила 4/6; AGENTS.md Architecture Boundaries).

Всё из `.env.example`, кроме compose-обвязки (POSTGRES_DB/POSTGRES_USER/POSTGRES_PASSWORD —
читает docker-compose, Python их не использует). Секреты — SecretStr без дефолтов в коде
(fail-fast на старте, S00 правило 4); опциональные с дефолтом — только не-секретные
операционные параметры (таймаут шлюза, путь трейса).

Бизнес-код читает env только через Settings: `os.environ`/`os.getenv` вне этого модуля
запрещены (S00 «Запрещено», самопроверка grep).
"""

from pathlib import Path

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Все параметры приложения; валидация обязательных — при первом создании.

    DSN-строки — SecretStr: внутри пароль, repr/redaction не должен его утечь
    (ревью S03 Important-3). Значение — через `.get_secret_value()`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM-шлюз (OpenAI-совместимый, routerai.ru) — единственный наружный вызов ---
    llm_base_url: str
    llm_api_key: SecretStr
    # Правило 9 (S00): потенциально зависающий LLM-вызов — только с таймаутом.
    # Таймаут — отказ узла, НЕ сбой breaker (ARCH-GATE S03 W2, ADR-007 §4.3).
    llm_request_timeout_s: float = Field(default=15.0, gt=0)

    @field_validator("llm_base_url")
    @classmethod
    def _base_url_is_http(cls, value: str) -> str:
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("llm_base_url должен начинаться с http:// или https://")
        return value

    # --- Роли моделей (ADR-007 §4): имя модели — только из Settings, не хардкод ---
    model_cheap: str
    model_mid: str
    model_strong: str

    # --- Бюджет (ADR-007 §4.3): жёсткий кап на один запрос пользователя, ₽ ---
    budget_per_request_rub: float = Field(gt=0)

    # --- Цены ролей (₽/1М токенов, вход/выход; model-card; дрейф цен — R-C1) ---
    model_cheap_price_in: float = Field(ge=0)
    model_cheap_price_out: float = Field(ge=0)
    model_mid_price_in: float = Field(ge=0)
    model_mid_price_out: float = Field(ge=0)
    model_strong_price_in: float = Field(ge=0)
    model_strong_price_out: float = Field(ge=0)

    # --- Эмбеддер (model-card; реализация — S06) ---
    embedding_model: str

    # --- PostgreSQL (DSN'ы двух каналов — security.md §2.2; пароль внутри — SecretStr) ---
    postgres_dsn: SecretStr
    agent_ro_dsn: SecretStr
    agent_trace_dsn: SecretStr
    valkey_url: str

    # --- Neo4j (граф связей, ADR-003) ---
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: SecretStr

    # --- Пароли ролей БД (создаёт миграция S02; соединения — S13/S15; FIX-007) ---
    agent_ro_password: SecretStr
    agent_planner_password: SecretStr
    agent_trace_password: SecretStr
    registry_writer_password: SecretStr

    # --- Трейс (FR-11): JSONL вне git; дефолт — канонный путь наблюдаемости ---
    trace_jsonl_path: Path = Path(".harness/traces/trace.jsonl")


def load_settings() -> Settings:
    """Фабрика Settings с внятной ошибкой fail-fast на отсутствующих переменных.

    Неверные ЗНАЧЕНИЯ (не missing) отдаются как есть — ValidationError с локом поля.
    """
    try:
        return Settings()
    except ValidationError as exc:
        missing = sorted(
            {str(err["loc"][0]).upper() for err in exc.errors() if err["type"] == "missing"}
        )
        if not missing:
            raise
        raise RuntimeError(
            "Settings: отсутствуют обязательные переменные окружения: "
            + ", ".join(missing)
            + ". Скопируйте .env.example в .env и заполните значения."
        ) from exc
