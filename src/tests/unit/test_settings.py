"""Юнит-тесты Settings (S03 Task 1): fail-fast на обязательных, дефолты, секреты.

Тесты герметичны: окружение чистится от всех переменных Settings (conftest
подгружает реальный .env в os.environ), .env-файл исключается chdir'ом во
временную папку / явным `_env_file=None`.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from text_to_sql_agent.config.settings import LOCAL_LLM_BASE_URL, Settings, load_settings

# Полный перечень env-переменных Settings (синхрон с .env.example, без compose-блока)
ENV_NAMES = (
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_REQUEST_TIMEOUT_S",
    "LLM_REASONING_EFFORT",
    "EMBEDDING_BASE_URL",
    "MODEL_CHEAP",
    "MODEL_MID",
    "MODEL_STRONG",
    "BUDGET_PER_REQUEST_RUB",
    "MODEL_CHEAP_PRICE_IN",
    "MODEL_CHEAP_PRICE_OUT",
    "MODEL_MID_PRICE_IN",
    "MODEL_MID_PRICE_OUT",
    "MODEL_STRONG_PRICE_IN",
    "MODEL_STRONG_PRICE_OUT",
    "EMBEDDING_MODEL",
    "POSTGRES_DSN",
    "AGENT_RO_DSN",
    "AGENT_TRACE_DSN",
    "VALKEY_URL",
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_PASSWORD",
    "AGENT_RO_PASSWORD",
    "AGENT_PLANNER_PASSWORD",
    "AGENT_TRACE_PASSWORD",
    "REGISTRY_WRITER_PASSWORD",
    "TRACE_JSONL_PATH",
)

REQUIRED_ENV = {
    "LLM_BASE_URL": "https://gateway.test/api/v1",
    "LLM_API_KEY": "sk-test",
    "MODEL_CHEAP": "cheap-1",
    "MODEL_MID": "mid-1",
    "MODEL_STRONG": "strong-1",
    "BUDGET_PER_REQUEST_RUB": "2",
    "MODEL_CHEAP_PRICE_IN": "3.2",
    "MODEL_CHEAP_PRICE_OUT": "13",
    "MODEL_MID_PRICE_IN": "8",
    "MODEL_MID_PRICE_OUT": "12",
    "MODEL_STRONG_PRICE_IN": "299",
    "MODEL_STRONG_PRICE_OUT": "939",
    "EMBEDDING_MODEL": "embed-1",
    "POSTGRES_DSN": "postgresql://postgres:postgres@127.0.0.1:5432/agent",
    "AGENT_RO_DSN": "postgresql://agent_ro:agent_ro@127.0.0.1:5432/agent",
    "AGENT_TRACE_DSN": "postgresql://agent_trace:agent_trace@127.0.0.1:5432/agent",
    "VALKEY_URL": "redis://127.0.0.1:6379/0",
    "NEO4J_URI": "bolt://127.0.0.1:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "neo4j-pw",
    "AGENT_RO_PASSWORD": "agent_ro",
    "AGENT_PLANNER_PASSWORD": "agent_planner",
    "AGENT_TRACE_PASSWORD": "agent_trace",
    "REGISTRY_WRITER_PASSWORD": "registry_writer",
}


@pytest.fixture()
def clean_env(monkeypatch, tmp_path):
    """Герметичное окружение: без .env-файла и без переменных Settings."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # Settings(env_file=".env") не находит файл
    return monkeypatch


def _set_required(monkeypatch, **overrides: str) -> None:
    values = {**REQUIRED_ENV, **overrides}
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_fail_fast_lists_missing_vars(clean_env):
    with pytest.raises(RuntimeError) as exc:
        load_settings()
    message = str(exc.value)
    assert "LLM_API_KEY" in message
    assert "MODEL_CHEAP" in message
    assert ".env.example" in message  # внятная подсказка, не сырой ValidationError


def test_load_ok_defaults_and_mapping(clean_env):
    _set_required(clean_env)
    settings = load_settings()
    assert settings.model_cheap == "cheap-1"  # MODEL_CHEAP → model_cheap
    assert settings.budget_per_request_rub == 2.0
    assert settings.llm_api_key.get_secret_value() == "sk-test"
    # Не-секретные операционные параметры — единственные с дефолтом в коде
    assert settings.llm_request_timeout_s == 15.0
    assert settings.trace_jsonl_path == Path(".harness/traces/trace.jsonl")


def test_secret_never_leaks_into_repr(clean_env):
    _set_required(clean_env)
    settings = load_settings()
    assert "sk-test" not in repr(settings)
    assert "sk-test" not in str(settings)
    assert "neo4j-pw" not in repr(settings)
    # DSN содержат пароль — тоже SecretStr (ревью S03 Important-3)
    assert "postgres:postgres@" not in repr(settings)
    assert "agent_ro:agent_ro@" not in repr(settings)


def test_compose_only_vars_ignored(clean_env):
    _set_required(clean_env)
    clean_env.setenv("POSTGRES_DB", "agent")
    clean_env.setenv("POSTGRES_USER", "postgres")
    clean_env.setenv("POSTGRES_PASSWORD", "postgres")
    settings = load_settings()  # extra="ignore" — compose-блок не ломает Settings
    assert settings.postgres_dsn.get_secret_value().endswith("/agent")


def test_non_positive_budget_rejected(clean_env):
    _set_required(clean_env, BUDGET_PER_REQUEST_RUB="0")
    with pytest.raises(ValidationError):
        load_settings()


def test_agent_planner_dsn_swaps_credentials(clean_env):
    # ruling W1 (S06): retrieval — под ролью agent_planner (c4-l2 v1.8);
    # пароль — из Settings с URL-экранированием, сервисный пароль не утекает
    _set_required(
        clean_env,
        POSTGRES_DSN="postgresql://postgres:service-pw@dbhost:5433/agentdb",
        AGENT_PLANNER_PASSWORD="pl@nner:pw",
    )
    dsn = load_settings().agent_planner_dsn
    assert dsn == "postgresql://agent_planner:pl%40nner%3Apw@dbhost:5433/agentdb"
    assert "service-pw" not in dsn


def test_non_positive_timeout_rejected(clean_env):
    _set_required(clean_env, LLM_REQUEST_TIMEOUT_S="0")
    with pytest.raises(ValidationError):
        load_settings()


def test_base_url_must_be_http(clean_env):
    _set_required(clean_env, LLM_BASE_URL="routerai.ru/api/v1")
    with pytest.raises(ValidationError):
        load_settings()


def test_base_url_empty_falls_back_to_local_lm_studio(clean_env):
    # D-020: пустой LLM_BASE_URL → локальный OpenAI-совместимый шлюз LM Studio
    _set_required(clean_env, LLM_BASE_URL="")
    assert load_settings().llm_base_url == LOCAL_LLM_BASE_URL


def test_base_url_missing_falls_back_to_local_lm_studio(clean_env):
    for key, value in REQUIRED_ENV.items():
        if key != "LLM_BASE_URL":
            clean_env.setenv(key, value)
    assert load_settings().llm_base_url == LOCAL_LLM_BASE_URL


def test_base_url_trailing_slash_stripped(clean_env):
    _set_required(clean_env, LLM_BASE_URL="https://routerai.ru/api/v1/")
    assert load_settings().llm_base_url == "https://routerai.ru/api/v1"


def test_embedding_base_url_empty_falls_back_to_local(clean_env):
    # D-025: пустой EMBEDDING_BASE_URL → локальный LM Studio (bge-m3, S06)
    _set_required(clean_env, EMBEDDING_BASE_URL="")
    assert load_settings().embedding_base_url == LOCAL_LLM_BASE_URL


def test_embedding_base_url_trailing_slash_stripped(clean_env):
    _set_required(clean_env, EMBEDDING_BASE_URL="https://routerai.ru/api/v1/")
    assert load_settings().embedding_base_url == "https://routerai.ru/api/v1"


def test_embedding_base_url_must_be_http(clean_env):
    _set_required(clean_env, EMBEDDING_BASE_URL="routerai.ru/api/v1")
    with pytest.raises(ValidationError):
        load_settings()


def test_embedding_base_url_without_host_rejected(clean_env):
    _set_required(clean_env, EMBEDDING_BASE_URL="https:///api/v1")
    with pytest.raises(ValidationError):
        load_settings()


def test_non_ascii_api_key_rejected(clean_env):
    # Заголовок Authorization — ASCII: кириллический плейсхолдер должен падать
    # на старте, а не UnicodeEncodeError'ом при сборке HTTP-запроса (smoke D-020)
    _set_required(clean_env, LLM_API_KEY="ЗАПОЛНИТЬ")
    with pytest.raises(ValidationError):
        load_settings()


def test_empty_api_key_rejected(clean_env):
    _set_required(clean_env, LLM_API_KEY="")
    with pytest.raises(ValidationError):
        load_settings()


def test_whitespace_only_api_key_rejected(clean_env):
    _set_required(clean_env, LLM_API_KEY="   ")
    with pytest.raises(ValidationError):
        load_settings()


def test_control_char_api_key_rejected(clean_env):
    # Управляющие байты в заголовке Authorization недопустимы — fail-fast на старте
    _set_required(clean_env, LLM_API_KEY="sk\x01bad")
    with pytest.raises(ValidationError):
        load_settings()


def test_api_key_never_leaks_into_validation_error(clean_env):
    # hide_input_in_errors: текст ValidationError не печатает сырой ключ (REVIEW-GATE D-020)
    _set_required(clean_env, LLM_API_KEY="НЕСЕКРЕТНЫЙ-КЛЮЧ-12345")
    with pytest.raises(ValidationError) as exc:
        load_settings()
    assert "НЕСЕКРЕТНЫЙ-КЛЮЧ-12345" not in str(exc.value)


def test_whitespace_only_base_url_falls_back_to_local(clean_env):
    _set_required(clean_env, LLM_BASE_URL="   ")
    assert load_settings().llm_base_url == LOCAL_LLM_BASE_URL


def test_timeout_override_applies(clean_env):
    _set_required(clean_env, LLM_REQUEST_TIMEOUT_S="30")
    assert load_settings().llm_request_timeout_s == 30.0


def test_explicit_kwargs_win_over_env(clean_env):
    _set_required(clean_env, MODEL_MID="mid-from-env")
    overrides = {k.lower(): v for k, v in REQUIRED_ENV.items()} | {"model_mid": "mid-explicit"}
    settings = Settings(_env_file=None, **overrides)
    assert settings.model_mid == "mid-explicit"


def test_reasoning_effort_normalized_and_allowed() -> None:
    # Re-review S25 I2: нормализация strip().lower() — «none » из .env не даёт
    # HTTP 400 на каждом вызове; канонический набор SDK пропускается
    settings = Settings(**REQUIRED_ENV, _env_file=None, llm_reasoning_effort=" none ")
    assert settings.llm_reasoning_effort == "none"
    settings = Settings(**REQUIRED_ENV, _env_file=None, llm_reasoning_effort="HIGH")
    assert settings.llm_reasoning_effort == "high"
    settings = Settings(**REQUIRED_ENV, _env_file=None, llm_reasoning_effort="  ")
    assert settings.llm_reasoning_effort == ""  # пусто — параметр не отправляется


def test_reasoning_effort_typo_fails_fast() -> None:
    # Опечатка падает на загрузке Settings, а не на первом запросе шлюза
    # (HTTP 400 не ретраится — деградация с невнятной причиной)
    with pytest.raises(ValidationError, match="канонического набора"):
        Settings(**REQUIRED_ENV, _env_file=None, llm_reasoning_effort="non")


def test_reasoning_effort_env_binding() -> None:
    env = {**REQUIRED_ENV, "LLM_REASONING_EFFORT": "low"}
    settings = Settings(_env_file=None, **env)
    assert settings.llm_reasoning_effort == "low"


def test_fsm_caps_defaults_are_canon() -> None:
    # Глобальные капы FSM-прогона (S04, state-agent v1.5): ≤ 8 вызовов / ≤ 90 s;
    # recursion_limit — запас над худшими ретраями (обоснование — settings.py)
    settings = Settings(**REQUIRED_ENV, _env_file=None)
    assert settings.fsm_max_llm_calls == 8
    assert settings.fsm_deadline_s == 90.0
    assert settings.fsm_recursion_limit == 50


def test_fsm_caps_reject_nonpositive() -> None:
    # Капы — предохранители: ноль/отрицательное сделали бы гвард неработающим
    with pytest.raises(ValidationError):
        Settings(**REQUIRED_ENV, _env_file=None, fsm_max_llm_calls=0)
    with pytest.raises(ValidationError):
        Settings(**REQUIRED_ENV, _env_file=None, fsm_deadline_s=-1)
    with pytest.raises(ValidationError):
        Settings(**REQUIRED_ENV, _env_file=None, fsm_recursion_limit=0)
