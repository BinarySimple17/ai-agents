"""Юнит-тесты Settings (S03 Task 1): fail-fast на обязательных, дефолты, секреты.

Тесты герметичны: окружение чистится от всех переменных Settings (conftest
подгружает реальный .env в os.environ), .env-файл исключается chdir'ом во
временную папку / явным `_env_file=None`.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from text_to_sql_agent.config.settings import Settings, load_settings

# Полный перечень env-переменных Settings (синхрон с .env.example, без compose-блока)
ENV_NAMES = (
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_REQUEST_TIMEOUT_S",
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


def test_non_positive_timeout_rejected(clean_env):
    _set_required(clean_env, LLM_REQUEST_TIMEOUT_S="0")
    with pytest.raises(ValidationError):
        load_settings()


def test_base_url_must_be_http(clean_env):
    _set_required(clean_env, LLM_BASE_URL="routerai.ru/api/v1")
    with pytest.raises(ValidationError):
        load_settings()


def test_timeout_override_applies(clean_env):
    _set_required(clean_env, LLM_REQUEST_TIMEOUT_S="30")
    assert load_settings().llm_request_timeout_s == 30.0


def test_explicit_kwargs_win_over_env(clean_env):
    _set_required(clean_env, MODEL_MID="mid-from-env")
    overrides = {k.lower(): v for k, v in REQUIRED_ENV.items()} | {"model_mid": "mid-explicit"}
    settings = Settings(_env_file=None, **overrides)
    assert settings.model_mid == "mid-explicit"
