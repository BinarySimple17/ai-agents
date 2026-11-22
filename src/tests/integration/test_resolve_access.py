"""Интеграционные тесты resolve_access (feat-005, S05): права из таблиц настроек.

Сиды 0003: u_logist (logistics_viewer — 5 таблиц '*'), u_analyst (analyst —
orders/order_items/products), u_hr (hr_viewer — employees/bonuses). Чтение —
PgPermissionStore под agent_planner (SELECT-гранты 0002, ruling R1 ARCH-GATE
S05); правки настроек — admin-канал POSTGRES_DSN (прецедент test_infra_schema):
bump-триггеры инкрементируют счётчики → сервис перечитывает права на следующий
запрос (behavior feat-005). FSM — через собранный Container (Task 4): known →
done с картой прав в state; unknown/без identity → rejected БЕЗ единого
LLM-вызова. Скипаются без инфры с понятной причиной (conftest).
"""

import os
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from text_to_sql_agent.application.ports.auth_provider import AuthenticationError
from text_to_sql_agent.application.services.resolve_access_service import ResolveAccessService
from text_to_sql_agent.application.services.stub_nodes import initial_state
from text_to_sql_agent.composition.container import build_container
from text_to_sql_agent.config.settings import Settings
from text_to_sql_agent.infrastructure.persistence.dev_token_auth import DevTokenAuth
from text_to_sql_agent.infrastructure.persistence.pg_permission_store import PgPermissionStore

_DEMO_TOKENS = "u_analyst:dev-analyst-token,u_logist:dev-logist-token"


def _dsn(env_var: str) -> str:
    dsn = os.getenv(env_var)
    if not dsn:
        pytest.skip(f"{env_var} не задан — скопируйте .env.example в .env")
    return dsn


@pytest.fixture(scope="session")
def planner_dsn(postgres_available) -> str:
    # Канал чтения прав — agent_planner (миграция 0002:80-83; ruling R1 S05)
    password = os.getenv("AGENT_PLANNER_PASSWORD")
    if not password:
        pytest.fail("AGENT_PLANNER_PASSWORD не задан — права agent_planner не проверяемы")
    return make_conninfo(_dsn("POSTGRES_DSN"), user="agent_planner", password=password)


@pytest.fixture(scope="function")
def admin_conn(postgres_available):
    # Канал правок настроек (bump-триггеры срабатывают любой роли с грантом)
    with psycopg.connect(_dsn("POSTGRES_DSN")) as conn:
        yield conn


def _settings(tmp_path: Path) -> Settings:
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
        postgres_dsn=SecretStr(_dsn("POSTGRES_DSN")),
        agent_ro_dsn=SecretStr(_dsn("AGENT_RO_DSN")),
        agent_trace_dsn=SecretStr(_dsn("AGENT_TRACE_DSN")),
        valkey_url="redis://127.0.0.1:6379/0",
        neo4j_uri="bolt://127.0.0.1:7687",
        neo4j_user="neo4j",
        neo4j_password=SecretStr("neo4j-dev-password"),
        agent_ro_password=SecretStr("agent_ro"),
        agent_planner_password=SecretStr(os.getenv("AGENT_PLANNER_PASSWORD", "agent_planner")),
        agent_trace_password=SecretStr("agent_trace"),
        registry_writer_password=SecretStr("registry_writer"),
        trace_jsonl_path=tmp_path / "trace-integration.jsonl",
        agent_dev_tokens=_DEMO_TOKENS,
    )


class TestDevTokenAuthIntegration:
    def test_demo_tokens_verify_to_seeded_users(self, tmp_path) -> None:
        auth = DevTokenAuth(_settings(tmp_path).dev_token_map)
        assert auth.verify("dev-analyst-token") == "u_analyst"
        assert auth.verify("dev-logist-token") == "u_logist"
        with pytest.raises(AuthenticationError, match="fail-closed"):
            auth.verify("dev-hr-token")  # u_hr не в демо-списке этой сборки


class TestPermissionStoreIntegration:
    def test_fetch_rights_known_users_by_seeds_0003(self, planner_dsn) -> None:
        store = PgPermissionStore(planner_dsn)
        analyst = store.fetch_rights("u_analyst")
        assert analyst.user_known is True
        assert analyst.roles == ("analyst",)
        assert analyst.tables == {"orders": ("*",), "order_items": ("*",), "products": ("*",)}
        logist = store.fetch_rights("u_logist")
        assert logist.roles == ("logistics_viewer",)
        assert set(logist.tables) == {"customers", "orders", "order_items", "campaigns", "products"}
        assert all(cols == ("*",) for cols in logist.tables.values())

    def test_fetch_rights_unknown_user_not_known(self, planner_dsn) -> None:
        store = PgPermissionStore(planner_dsn)
        rights = store.fetch_rights("integration-ghost")
        assert rights.user_known is False
        assert rights.tables == {}

    def test_versions_carry_both_counters(self, planner_dsn) -> None:
        versions = PgPermissionStore(planner_dsn).settings_versions()
        assert set(versions) == {"permissions", "user_roles"}  # ruling R3: ОБА счётчика


class TestRightsFreshness:
    """Правка строк настроек действует со следующего запроса (behavior feat-005)."""

    def test_permission_edit_invalidates_cache(self, planner_dsn, admin_conn) -> None:
        store = PgPermissionStore(planner_dsn)
        service = ResolveAccessService(store)
        before = service.resolve("u_logist")
        assert "employees" not in before.tables
        try:
            with admin_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent_permissions (role, table_name, columns, updated_by) "
                    "VALUES ('logistics_viewer', 'employees', '*', 'integration') "
                    "ON CONFLICT (role, schema_name, table_name) DO NOTHING"
                )
            admin_conn.commit()
            after = service.resolve("u_logist")  # bump 'permissions' → перечитывание
            assert "employees" in after.tables
        finally:
            with admin_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM agent_permissions "
                    "WHERE role = 'logistics_viewer' AND table_name = 'employees' "
                    "AND updated_by = 'integration'"
                )
            admin_conn.commit()

    def test_role_grant_invalidates_cache(self, planner_dsn, admin_conn) -> None:
        # Выдача роли инкрементирует 'user_roles' (ruling R3: кэш по одному
        # счётчику пропустил бы revoke/выдачу роли)
        store = PgPermissionStore(planner_dsn)
        service = ResolveAccessService(store)
        before = service.resolve("integration-ghost")
        assert before.user_known is False
        try:
            with admin_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent_user_roles (user_id, role) "
                    "VALUES ('integration-ghost', 'analyst') "
                    "ON CONFLICT (user_id, role) DO NOTHING"
                )
            admin_conn.commit()
            after = service.resolve("integration-ghost")
            assert after.user_known is True
            assert after.tables == {"orders": ("*",), "order_items": ("*",), "products": ("*",)}
        finally:
            with admin_conn.cursor() as cur:
                cur.execute("DELETE FROM agent_user_roles WHERE user_id = 'integration-ghost'")
            admin_conn.commit()

    def test_role_revoke_invalidates_cache(self, planner_dsn, admin_conn) -> None:
        # Отзыв роли — опасное направление (стейл-ПОВЫШЕННЫЕ права, ревью S05
        # Minor-8): DELETE в agent_user_roles инкрементирует 'user_roles' →
        # следующий запрос перечитывает права и теряет выданные таблицы
        store = PgPermissionStore(planner_dsn)
        service = ResolveAccessService(store)
        try:
            with admin_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent_user_roles (user_id, role) "
                    "VALUES ('integration-ghost', 'analyst') "
                    "ON CONFLICT (user_id, role) DO NOTHING"
                )
            admin_conn.commit()
            granted = service.resolve("integration-ghost")
            assert granted.user_known is True
            assert "orders" in granted.tables
            with admin_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM agent_user_roles "
                    "WHERE user_id = 'integration-ghost' AND role = 'analyst'"
                )
            admin_conn.commit()
            revoked = service.resolve("integration-ghost")
            assert revoked.user_known is False
            assert revoked.tables == {}
        finally:
            with admin_conn.cursor() as cur:
                cur.execute("DELETE FROM agent_user_roles WHERE user_id = 'integration-ghost'")
            admin_conn.commit()


class TestFSMThroughContainer:
    """Task 4: FSM-путь жив — тест через собранный Container, не напрямую сервис."""

    @pytest.fixture()
    def container(self, tmp_path, postgres_available):
        return build_container(_settings(tmp_path))

    def test_known_user_done_with_permissions_in_state(self, container) -> None:
        state = initial_state("Сколько заказов у клиентов из РФ?", user_id="u_analyst")
        result = container.fsm_runner.run(state)
        assert result["outcome"] == "done"
        assert result["permissions"] == {
            "orders": ["*"],
            "order_items": ["*"],
            "products": ["*"],
        }
        assert result["visited_nodes"][0] == "resolve_access"

    def test_unknown_user_rejected_without_llm_calls(self, container) -> None:
        # Fail-closed (ruling R6) + behavior feat-005: rejected ДО первого
        # LLM-узла — ни одного вызова шлюза
        state = initial_state("Сколько заказов?", user_id="integration-ghost")
        result = container.fsm_runner.run(state)
        assert result["outcome"] == "rejected"
        assert result["budget_used"]["llm_calls"] == 0
        assert result["error"]["class"] == "unknown_user"

    def test_missing_identity_rejected_without_llm_calls(self, container) -> None:
        # Сырой user_id не проходит (R4): identity в state не пришла — rejected
        state = initial_state("Сколько заказов?")
        result = container.fsm_runner.run(state)
        assert result["outcome"] == "rejected"
        assert result["budget_used"]["llm_calls"] == 0
        assert result["error"]["class"] == "identity_missing"

    def test_auth_verify_roundtrip_into_fsm(self, container) -> None:
        # Полный контур R4: токен → verify → user_id → FSM → done с правами
        user_id = container.auth_provider.verify("dev-analyst-token")
        assert user_id == "u_analyst"
        result = container.fsm_runner.run(initial_state("Сколько заказов?", user_id=user_id))
        assert result["outcome"] == "done"
        assert result["permissions"]
