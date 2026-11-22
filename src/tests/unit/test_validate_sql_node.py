"""Юнит-тесты узла validate_sql (feat-011, S11): glue чистый валидатор ↔ state.

Валидатор детерминированный (без LLM/IO) — сервис реальный; каталог мира —
фейк-provider (счётчик вызовов: инвариант «на каждый запрос», S00 стык 3).
Ветвление FSM — через routing.route_validate_sql (рёбра графа — тонкие функции
над route_flags, S04): три исхода вехи ADR-005 §4 → три маршрута (check_cost /
diagnose_error / degrade). Parity имён флагов — application не импортирует
orchestration (прецедент GENERATE_SQL_FAILED_FLAG S10).
"""

import pytest

from text_to_sql_agent.application.ports.permission_store import RightsUnavailableError
from text_to_sql_agent.application.ports.sql_executor import ROW_LIMIT
from text_to_sql_agent.application.ports.tracer import (
    EVENT_VALIDATE_SQL_UNAVAILABLE,
    EVENT_VALIDATE_SQL_VERDICT,
)
from text_to_sql_agent.application.services.validate_sql_node import (
    ACL_DENIED_FLAG,
    VALIDATE_HALLUCINATION_FLAG,
    make_validate_sql_node,
)
from text_to_sql_agent.application.services.validate_sql_service import ValidateSqlService
from text_to_sql_agent.domain.join_plan import JoinPlan, JoinPlanEdge
from text_to_sql_agent.domain.skeleton import build_skeleton
from text_to_sql_agent.orchestration import routing

_SERVICE = ValidateSqlService()

# Каноничный мир (контракт стыка 3): права списками колонок, каталог — SoT мира
PERMISSIONS = {
    "orders": ["id", "customer_id", "total", "status"],
    "customers": ["id", "name", "region"],
}
CATALOG = frozenset({"orders", "customers", "bonuses", "campaigns", "order_items"})


def _plan() -> JoinPlan:
    return JoinPlan(
        tables=["orders", "customers"],
        edges=[
            JoinPlanEdge(
                rel_id="rel-orders-customers",
                from_table="orders",
                to_table="customers",
                from_columns=("customer_id",),
                to_columns=("id",),
                join_type="inner",
            )
        ],
        graph_version=1,
    )


def _state(**overrides):
    plan = overrides.pop("plan", None) or _plan()
    skeleton = build_skeleton(plan)
    state = {
        "user_id": "u_analyst",
        "sql": ("SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id"),
        "skeleton": skeleton.model_dump(),
        "skeleton_hash": skeleton.skeleton_hash,
        "join_plan": plan.model_dump(),
        "permissions": {k: list(v) for k, v in PERMISSIONS.items()},
        "route_flags": {},
    }
    state.update(overrides)
    return state


def _node(catalog=CATALOG, tracer=None):
    return make_validate_sql_node(_SERVICE, catalog_provider=lambda: catalog, tracer=tracer)


class TestOkOutcome:
    def test_ok_routes_to_check_cost(self):
        update = _node()(_state())
        assert update["validation"]["outcome"] == "ok"
        assert update["validation"]["issues"] == []
        assert update["route_flags"][VALIDATE_HALLUCINATION_FLAG] is False
        assert update["route_flags"][ACL_DENIED_FLAG] is False
        assert update["error"] is None
        merged = {**_state(), **update}
        assert routing.route_validate_sql(merged) == "check_cost"

    def test_sql_rewritten_with_enforced_limit(self):
        # ruling R1: исполняемый текст несёт внешний LIMIT; кламп — enforced_limit
        update = _node()(_state())
        assert "LIMIT" in update["sql"].upper()
        assert update["validation"]["enforced_limit"] == ROW_LIMIT

    def test_star_expanded_before_execution(self):
        # ruling R2: литеральная звезда не доходит до исполнителя (ревью M2:
        # в входе ДОЛЖНА быть звезда — иначе тест кусает вхолостую)
        update = _node()(
            _state(
                sql="SELECT * FROM orders o INNER JOIN customers c ON o.customer_id = c.id",
                permissions={"orders": ["id", "customer_id"], "customers": ["id", "region"]},
            )
        )
        assert update["validation"]["outcome"] == "ok"
        assert "o.id" in update["sql"] and "o.customer_id" in update["sql"]
        assert "*" not in update["sql"]


class TestHallucinationOutcome:
    def test_unknown_table_routes_to_diagnose_error(self):
        # sales_2025 вне каталога мира — «не существует», retry оправдан
        update = _node()(
            _state(sql="SELECT s.x FROM sales_2025 s INNER JOIN customers c ON s.id = c.id")
        )
        assert update["validation"]["outcome"] == "hallucination"
        assert update["route_flags"][VALIDATE_HALLUCINATION_FLAG] is True
        assert update["route_flags"][ACL_DENIED_FLAG] is False
        assert update["error"]["class"] == "validate_hallucination"
        merged = {**_state(), **update}
        assert routing.route_validate_sql(merged) == "diagnose_error"

    def test_hallucination_user_message_without_table_names(self):
        # ревью S11 I1: имена таблиц/инфраструктуры — только во внутреннем
        # validation.issues (контекст ретрая M1; подключение — S14), а
        # пользовательский текст (state.error.message, печатает CLI) —
        # фиксированный бизнес-текст (S9: отказ не раскрывает конфигурацию)
        update = _node()(
            _state(sql="SELECT id FROM orders o INNER JOIN customers c ON o.customer_id = c.id")
        )
        assert update["validation"]["outcome"] == "hallucination"
        assert update["validation"]["issues"]
        assert "orders" not in update["error"]["message"]
        assert "customers" not in update["error"]["message"]

    def test_sql_not_rewritten_on_rejection(self):
        # отказ: узел не пишет sql (LangGraph-merge оставляет исходный текст —
        # исполняться он не будет, маршруты ведут в diagnose/degrade)
        sql = "SELECT s.x FROM sales_2025 s INNER JOIN customers c ON s.id = c.id"
        update = _node()(_state(sql=sql))
        assert "sql" not in update
        assert {**_state(sql=sql), **update}["sql"] == sql


class TestDeniedOutcome:
    def test_denied_routes_to_degrade_without_retry(self):
        # bonuses существует в мире, но вне прав — сигнал безопасности, БЕЗ retry
        update = _node()(
            _state(sql="SELECT b.amount FROM bonuses b INNER JOIN customers c ON b.id = c.id")
        )
        assert update["validation"]["outcome"] == "denied"
        assert update["route_flags"][ACL_DENIED_FLAG] is True
        assert update["route_flags"][VALIDATE_HALLUCINATION_FLAG] is False
        assert update["error"]["class"] == "acl_denied"
        merged = {**_state(), **update}
        assert routing.route_validate_sql(merged) == "degrade"

    def test_denied_user_message_without_table_names(self):
        # S9: отказ не раскрывает существование данных — имя таблицы только в
        # внутреннем контексте (validation.issues), не в пользовательском тексте
        update = _node()(
            _state(sql="SELECT b.amount FROM bonuses b INNER JOIN customers c ON b.id = c.id")
        )
        assert "bonuses" not in update["error"]["message"]


class TestFailClosedDefects:
    """Дефект каркаса/стыка → denied-маршрут (degrade без retry), не crash."""

    @pytest.mark.parametrize(
        ("mutate", "expected_class"),
        [
            ({"user_id": ""}, "identity_missing"),
            ({"sql": None}, "sql_missing"),
            ({"sql": "   "}, "sql_missing"),
            ({"skeleton": None}, "skeleton_invalid"),
            ({"skeleton": {"tables": "мусор"}}, "skeleton_invalid"),
            ({"join_plan": None}, "join_plan_invalid"),
            ({"skeleton_hash": ""}, "skeleton_hash_missing"),
            ({"permissions": None}, "permissions_missing"),
        ],
    )
    def test_missing_or_broken_inputs(self, mutate, expected_class):
        update = _node()(_state(**mutate))
        assert update["error"]["class"] == expected_class
        assert update["validation"]["outcome"] == "denied"
        assert update["route_flags"][ACL_DENIED_FLAG] is True
        assert update["route_flags"][VALIDATE_HALLUCINATION_FLAG] is False
        merged = {**_state(), **update}
        assert routing.route_validate_sql(merged) == "degrade"

    def test_catalog_unavailable_fail_closed(self):
        # S00-8: недоступный каталог — отказ, не «пустой мир = всё запрещено»
        def boom() -> frozenset[str]:
            raise RightsUnavailableError("db down")

        update = make_validate_sql_node(_SERVICE, catalog_provider=boom)(_state())
        assert update["error"]["class"] == "catalog_unavailable"
        assert update["validation"]["outcome"] == "denied"

    def test_csv_permissions_contract_fail_closed(self):
        # BL-014: str-value в правах → ValueError сервиса → дефект wiring,
        # fail-closed degrade, не «пропустили» и не traceback
        update = _node()(_state(permissions={"orders": "id,total", "customers": ["*"]}))
        assert update["error"]["class"] == "permissions_contract"
        assert update["validation"]["outcome"] == "denied"


class TestParityFlagsRouting:
    def test_flag_names_parity_with_routing(self):
        # application не импортирует orchestration — дубли имён гвардится parity,
        # иначе routing прочитает чужой ключ и маршрут разъедется (S04/S10)
        assert VALIDATE_HALLUCINATION_FLAG == routing.FLAG_VALIDATE_HALLUCINATION
        assert ACL_DENIED_FLAG == routing.FLAG_ACL_DENIED


class TestTraceAndCatalog:
    def test_verdict_event_recorded(self):
        events: list = []

        class _Tracer:
            def record(self, trace) -> None:
                events.append(trace)

        _node(tracer=_Tracer())(_state())
        assert events[0].event == EVENT_VALIDATE_SQL_VERDICT
        assert events[0].payload["outcome"] == "ok"
        # ревью M5: ключ — issues_count (счёт), не issues (массив) — имя не вводит
        assert events[0].payload["issues_count"] == 0
        assert "issues" not in events[0].payload
        assert "sql" not in events[0].payload  # D-022-аналог: БЕЗ SQL-текста

    def test_unavailable_event_on_catalog_failure(self):
        events: list = []

        class _Tracer:
            def record(self, trace) -> None:
                events.append(trace)

        def boom() -> frozenset[str]:
            raise RightsUnavailableError("db down")

        make_validate_sql_node(_SERVICE, catalog_provider=boom, tracer=_Tracer())(_state())
        assert events[0].event == EVENT_VALIDATE_SQL_UNAVAILABLE
        assert events[0].payload["class"] == "catalog_unavailable"

    def test_catalog_provider_called_per_invocation(self):
        # S00 стык 3: каталог мира читается на каждый запрос, не кэш узла
        calls = 0

        def provider() -> frozenset[str]:
            nonlocal calls
            calls += 1
            return CATALOG

        node = make_validate_sql_node(_SERVICE, catalog_provider=provider)
        node(_state())
        node(_state())
        assert calls == 2
