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
    # Гашение reasoning-канала шлюза (S25/feat-020, layer-3): qwen3.8-9b (D-020)
    # с тула́ми нередко не закрывает think-блок — весь ответ уходит в
    # reasoning_content и стримится ОДНИМ фолбэк-чанком (D-021/W4 S19), что
    # убивает потоковый вывод ДЗ-03. Значение из словаря reasoning_effort шлюза
    # ("none"/"low"/"medium"/"high" — LM Studio принимает "none"); пусто — не
    # отправлять вовсе (облачные шлюзы, где параметр нестандартный)
    llm_reasoning_effort: str = ""

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

    @field_validator("llm_reasoning_effort")
    @classmethod
    def _reasoning_effort_canonical(cls, value: str) -> str:
        # Fail-fast, как соседние валидаторы: опечатка/хвостовой пробел в .env
        # иначе дали бы HTTP 400 на КАЖДОМ вызове (400 не ретраится) — деградацию
        # с невнятной причиной (ревью S25 re-review I2). Allowlist — канонический
        # набор SDK ReasoningEffort; LM Studio дополнительно знает "on"/"off",
        # их "off" соответствует "none" (ревью владельцем настройки шлюза)
        normalized = value.strip().lower()
        if not normalized:
            return ""  # пусто — параметр не отправляется (дефолт)
        allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
        if normalized not in allowed:
            raise ValueError(
                f"llm_reasoning_effort={value!r} вне канонического набора {sorted(allowed)} "
                "(или пусто — параметр не отправляется)"
            )
        return normalized

    # --- Роли моделей (ADR-007 §4): имя модели — только из Settings, не хардкод ---
    model_cheap: str
    model_mid: str
    model_strong: str

    # --- Бюджет (ADR-007 §4.3): жёсткий кап на один запрос пользователя, ₽ ---
    budget_per_request_rub: float = Field(gt=0)

    # --- Глобальные капы FSM-прогона (S04/feat-004; state-agent v1.5, hw-01 §6) ---
    # Все три капы задаёт код, модель их не переопределяет (ADR-002 §4).
    # ≤ 8 LLM-вызовов на прогон: каждый ретрай генерации = 1 вызов (политика M1,
    # D-016 — до 3 попыток на узел) + содержательные узлы (clarify/plan_joins/
    # generate/diagnose/interpret); 8 — канонная верхняя граница нормального
    # прогона, теснее recursion_limit и срабатывает раньше по построению.
    fsm_max_llm_calls: int = Field(default=8, gt=0)
    # Дедлайн прогона 90 s (hw-01 §6): общий дедлайн «вопрос → ответ» (внутри —
    # 30 s на SQL-канал и таймаут шлюза из Settings.llm_request_timeout_s);
    # проверяет гвард на рёбрах перед узлами с LLM (routing.llm_guard)
    fsm_deadline_s: float = Field(default=90.0, gt=0)
    # recursion_limit LangGraph — второй уровень предохранителей (ADR-002 §4):
    # страховка от комбинаций ветвлений, которые наузловые лимиты не покрывают.
    # Почему 50: happy path ≈ 12 супер-шагов (11 узлов + терминал), худший случай
    # с ретраями ≈ 44 (plan_joins ×3 (+гейты) + цикл validate→diagnose→generate ×3
    # + execute→diagnose ×3 + probe) — 50 с запасом; независимый, более тесный
    # предел — fsm_max_llm_calls (без LLM-вызова узел не ретраится содержательно).
    fsm_recursion_limit: int = Field(default=50, gt=0)

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

    # --- Dev-аутентификация (S05, ruling R4 ARCH-GATE; ADR-005:114-117) ---
    # Токены демо-пользователей "user_id:token,user_id:token" (сидированные
    # u_analyst/u_logist/u_hr — миграция 0003); секрет — в .env, не в коде.
    # Production — OIDCAuth за тем же портом (вне скоупа MVP). Пусто —
    # DevTokenAuth отклоняет любой токен (fail-closed); CLI --fsm требует
    # --token (сырой --user verify не проходит).
    agent_dev_tokens: str = ""

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

    @property
    def dev_token_map(self) -> dict[str, str]:
        """``AGENT_DEV_TOKENS`` "user_id:token,…" → {user_id: token} (fail-fast на мусоре).

        Мусорная запись (нет ':', пустые части) и дубликат user_id — ValueError:
        конфигурация аутентификации не прощает опечаток — молча пропущенная пара
        означала бы fail-closed-отказ в рантайме там, где нужна демонстрация
        (fail-fast на старте процесса).
        """
        tokens: dict[str, str] = {}
        raw = self.agent_dev_tokens.strip()
        if not raw:
            return {}
        for entry in raw.split(","):
            user_id, sep, token = entry.strip().partition(":")
            if not sep or not user_id or not token:
                raise ValueError("AGENT_DEV_TOKENS: ожидается 'user_id:token' — обе части непусты")
            if user_id in tokens:
                raise ValueError(f"AGENT_DEV_TOKENS: дубликат user_id '{user_id}'")
            tokens[user_id] = token
        return tokens


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
