"""Единственная точка чтения окружения (S00 правила 4/6; AGENTS.md Architecture Boundaries).

Всё из `.env.example`, кроме compose-обвязки (POSTGRES_DB/POSTGRES_USER/POSTGRES_PASSWORD —
читает docker-compose, Python их не использует). Секреты — SecretStr без дефолтов в коде
(fail-fast на старте, S00 правило 4); опциональные с дефолтом — только не-секретные
операционные параметры (базовый URL шлюза — локальный LM Studio при пустом, D-020;
таймаут шлюза, путь трейса).

Бизнес-код читает env только через Settings: `os.environ`/`os.getenv` вне этого модуля
запрещены (S00 «Запрещено», самопроверка grep).
"""

from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import Field, SecretStr, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Дефолт пустого LLM_BASE_URL (решение владельца D-020): локальный OpenAI-совместимый
# шлюз LM Studio. Путь — именно /v1 (OpenAI-совместимый API); /api/v1 у LM Studio —
# собственный REST, на нём chat/completions отвечает 404 (проверено живым вызовом
# 2026-11-18). Хост — 127.0.0.1, НЕ localhost: тот же Windows-овощ, что в DSN-правиле
# S02 — IPv6 ::1 резолвится первым и молча дропается (+60s на коннект).
LOCAL_LLM_BASE_URL = "http://127.0.0.1:1234/v1"


class Settings(BaseSettings):
    """Все параметры приложения; валидация обязательных — при первом создании.

    DSN-строки — SecretStr: внутри пароль, repr/redaction не должен его утечь
    (ревью S03 Important-3). Значение — через `.get_secret_value()`.
    hide_input_in_errors: ValidationError не печатает сырое значение поля —
    иначе fail-fast на кривом ключе напечатал бы сам секрет (REVIEW-GATE D-020).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,  # секрет не попадает в текст ValidationError
    )

    # --- LLM-шлюз (OpenAI-совместимый: облако routerai.ru или локальный LM Studio) ---
    # Пусто или не задано → локальный LM Studio (D-020): dev-контур без облака.
    llm_base_url: str = ""
    llm_api_key: SecretStr  # LM Studio ключ не проверяет — любой непустой ASCII-плейсхолдер
    # Правило 9 (S00): потенциально зависающий LLM-вызов — только с таймаутом.
    # Таймаут — отказ узла, НЕ сбой breaker (ARCH-GATE S03 W2, ADR-007 §4.3).
    llm_request_timeout_s: float = Field(default=15.0, gt=0)

    @field_validator("llm_base_url", "embedding_base_url")
    @classmethod
    def _base_url_default_or_http(cls, value: str, info: ValidationInfo) -> str:
        # Один паттерн для обоих OpenAI-совместимых шлюзов (D-020/D-025): пусто →
        # локальный LM Studio; эмбеддер ходит в тот же endpoint по умолчанию.
        url = value.strip()
        if not url:
            return LOCAL_LLM_BASE_URL
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError(
                f"{info.field_name} должен начинаться с http:// или https:// "
                "(или быть пустым — тогда используется локальный LM Studio)"
            )
        url = url.rstrip("/")  # хвостовой «/» дал бы «//chat/completions» у SDK
        if not urlsplit(url).netloc:
            raise ValueError(
                f"{info.field_name} без хоста: {url!r} (пример: https://routerai.ru/api/v1)"
            )
        return url

    @field_validator("llm_api_key")
    @classmethod
    def _api_key_is_header_safe(cls, value: SecretStr) -> SecretStr:
        # Заголовок Authorization кодируется ASCII: кириллица падала не на старте,
        # а UnicodeEncodeError'ом при сборке HTTP-запроса (живой smoke D-020).
        raw = value.get_secret_value().strip()
        if not raw:
            raise ValueError(
                "llm_api_key не должен быть пустым "
                "(LM Studio ключ не проверяет — годится любой непустой ASCII-плейсхолдер)"
            )
        try:
            raw.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError(
                "llm_api_key должен быть ASCII-строкой: кириллица в ключе ломает "
                "заголовок Authorization (для локального LM Studio — плейсхолдер, "
                "например sk-local)"
            ) from None
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
            raise ValueError(
                "llm_api_key содержит управляющие символы — не годится для заголовка Authorization"
            )
        return SecretStr(raw)

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
    # Endpoint эмбеддера — по паттерну D-020/D-025: пусто → локальный LM Studio
    # (bge-m3); авторизация — общий LLM_API_KEY, отдельный секрет не вводится.
    embedding_base_url: str = ""
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

    @property
    def agent_planner_dsn(self) -> str:
        """DSN канала retrieval (ruling W1 ARCH-GATE S06): учётка POSTGRES_DSN →
        agent_planner/AGENT_PLANNER_PASSWORD.

        c4-l2 v1.8: «пул agent_planner: retrieval, настройки, EXPLAIN» — не
        сервисный DSN и не agent_ro (роль чтения прав). Пароль хранится отдельно
        (SecretStr), в DSN — только подстановка; спецсимволы URL-экранируются.
        Возвращает str с паролем внутри (DSN-строка для psycopg.connect) — не
        логировать/repr-ить: правило секретов модуля (ревью S06 Minor-8).
        """
        parts = urlsplit(self.postgres_dsn.get_secret_value())
        password = quote(self.agent_planner_password.get_secret_value(), safe="")
        netloc = f"agent_planner:{password}@{parts.netloc.rpartition('@')[2]}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


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
