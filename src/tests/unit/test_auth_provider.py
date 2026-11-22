"""Юнит-тесты identity (S05 Task 1): DevTokenAuth + Settings.dev_token_map.

Fail-closed-контракт порта AuthProvider (ruling R4 ARCH-GATE S05): identity —
только из проверенного токена; пустой/неизвестный — `AuthenticationError`,
дефолтного пользователя нет. Токены — из Settings (``AGENT_DEV_TOKENS``),
парсер fail-fast на мусоре/дубликатах.
"""

import pytest
from pydantic import SecretStr

from text_to_sql_agent.application.ports.auth_provider import AuthenticationError, AuthProvider
from text_to_sql_agent.config.settings import Settings
from text_to_sql_agent.infrastructure.persistence.dev_token_auth import DevTokenAuth

_TOKENS = {"u_analyst": "dev-analyst-token", "u_logist": "dev-logist-token"}


class TestDevTokenAuth:
    def test_verify_valid_token_returns_verified_identity(self) -> None:
        auth = DevTokenAuth(_TOKENS)
        assert auth.verify("dev-analyst-token") == "u_analyst"
        assert auth.verify("dev-logist-token") == "u_logist"

    def test_verify_unknown_token_rejected(self) -> None:
        auth = DevTokenAuth(_TOKENS)
        with pytest.raises(AuthenticationError):
            auth.verify("dev-hr-token")  # токен другого пользователя

    def test_verify_empty_and_whitespace_token_rejected(self) -> None:
        auth = DevTokenAuth(_TOKENS)
        with pytest.raises(AuthenticationError):
            auth.verify("")
        with pytest.raises(AuthenticationError):
            auth.verify("   ")

    def test_verify_with_empty_token_map_rejected(self) -> None:
        # Пустая карта (AGENT_DEV_TOKENS не задан) — отклоняется любой токен
        auth = DevTokenAuth({})
        with pytest.raises(AuthenticationError):
            auth.verify("dev-analyst-token")

    def test_implements_auth_provider_protocol(self) -> None:
        assert isinstance(DevTokenAuth(_TOKENS), AuthProvider)


def _settings(tokens: str) -> Settings:
    """Герметичные Settings с явными полями (прецедент test_container_wiring)."""
    return Settings(
        _env_file=None,
        llm_api_key=SecretStr("sk-test"),
        model_cheap="cheap",
        model_mid="mid",
        model_strong="strong",
        budget_per_request_rub=2.0,
        model_cheap_price_in=0,
        model_cheap_price_out=0,
        model_mid_price_in=0,
        model_mid_price_out=0,
        model_strong_price_in=0,
        model_strong_price_out=0,
        embedding_model="embed-1",
        postgres_dsn="postgresql://postgres:pw@127.0.0.1:5432/agent",
        agent_ro_dsn="postgresql://agent_ro:pw@127.0.0.1:5432/agent",
        agent_trace_dsn="postgresql://agent_trace:pw@127.0.0.1:5432/agent",
        valkey_url="redis://127.0.0.1:6379/0",
        neo4j_uri="bolt://127.0.0.1:7687",
        neo4j_user="neo4j",
        neo4j_password=SecretStr("pw"),
        agent_ro_password=SecretStr("agent_ro"),
        agent_planner_password=SecretStr("agent_planner"),
        agent_trace_password=SecretStr("agent_trace"),
        registry_writer_password=SecretStr("registry_writer"),
        agent_dev_tokens=tokens,
    )


class TestDevTokenMapParsing:
    def test_parse_valid_csv(self) -> None:
        settings = _settings("u_analyst:tok-a, u_logist:tok-b")
        assert settings.dev_token_map == {"u_analyst": "tok-a", "u_logist": "tok-b"}

    def test_empty_value_empty_map(self) -> None:
        assert _settings("").dev_token_map == {}

    @pytest.mark.parametrize("raw", ["no-colon-here", "u_analyst:", ":tok", "u:  "])
    def test_malformed_entry_fail_fast(self, raw: str) -> None:
        settings = _settings(raw)
        with pytest.raises(ValueError, match="AGENT_DEV_TOKENS"):
            settings.dev_token_map  # noqa: B018 — доступ к property и есть триггер парсера

    def test_duplicate_user_id_fail_fast(self) -> None:
        settings = _settings("u_analyst:tok-a,u_analyst:tok-b")
        with pytest.raises(ValueError, match="дубликат"):
            settings.dev_token_map  # noqa: B018 — доступ к property и есть триггер парсера

    def test_verify_roundtrip_through_settings(self) -> None:
        # Сборка composition: DevTokenAuth(settings.dev_token_map) — сквозной контракт
        auth = DevTokenAuth(_settings("u_analyst:tok-a").dev_token_map)
        assert auth.verify("tok-a") == "u_analyst"
