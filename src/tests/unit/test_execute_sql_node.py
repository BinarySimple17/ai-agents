"""Юнит-тесты узла execute_sql (S13 Task 3 + S14 Task 2): сервис, FSM-glue, probe COUNT.

LLM в тестах не участвует (узел чистый код): двойник SQLExecutor — очередь
результатов/ошибок (probe делает несколько COUNT-запросов одним каналом),
RecordingTracer — проверка execute.probe/unavailable (ruling R5 ARCH-GATE S14).
Маршруты маршрутизирует routing.route_execute_sql (единственный источник,
ruling R4: заготовка execute_sql_route не подключается к графу).
"""

from typing import Any

from tests.unit.fakes import RecordingTracer
from text_to_sql_agent.application.ports.sql_executor import (
    QueryRequest,
    QueryResult,
    SQLExecutionError,
    SQLFailureClass,
)
from text_to_sql_agent.application.services.execute_sql_node import (
    EXECUTE_DEFECT_FLAG,
    PROBE_LOST_JOIN_FLAG,
    ExecuteSqlNode,
    make_execute_sql_node,
)
from text_to_sql_agent.domain.entities import RunContext

UNIT_CTX = RunContext(trace_id="unit-s14", user_id="tester")

_STEP_FLAG_NAMES = ("step_done", "steps_pending", "step_empty_keyset", "step_keyset_overflow")

_SKELETON = {
    "tables": ("orders", "customers"),
    "from_table": "orders",
    "join_sections": ("JOIN customers ON orders.customer_id = customers.id",),
    "mandatory_predicates": (),
    "skeleton_hash": "h",
}


class ScriptedExecutor:
    """Двойник SQLExecutor: очередь исходов (основной запрос + COUNT-пробы)."""

    def __init__(self, script: list[QueryResult | SQLExecutionError]) -> None:
        self.script = list(script)
        self.requests: list[QueryRequest] = []
        self.ctxs: list[RunContext] = []

    def execute(self, request: QueryRequest, *, ctx: RunContext) -> QueryResult:
        self.requests.append(request)
        self.ctxs.append(ctx)
        item = self.script.pop(0)
        if isinstance(item, SQLExecutionError):
            raise item
        return item


def _rows(n: int) -> QueryResult:
    return QueryResult(columns=["id"], rows=[{"id": i} for i in range(n)], row_count=n)


def _count(n: int) -> QueryResult:
    return QueryResult(columns=["probe_count"], rows=[{"probe_count": n}], row_count=1)


def _base_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "user_id": "u1",
        "trace_id": "tr-1",
        "sql": "SELECT count(*) FROM orders JOIN customers ON orders.customer_id = customers.id",
        "skeleton": dict(_SKELETON),
        "route_flags": {},
        "error": None,
    }
    state.update(overrides)
    return state


# --- Сервис ExecuteSqlNode (S13, без изменений контракта) ---


def test_service_success_lays_result_into_state() -> None:
    executor = ScriptedExecutor([_rows(1)])
    state = ExecuteSqlNode(executor).run({"sql": "SELECT 1"}, ctx=UNIT_CTX)
    assert state["error"] is None
    assert state["row_count"] == 1
    assert state["rows"] == [{"id": 0}]
    assert state["truncated"] is False


def test_service_error_is_structured_not_traceback() -> None:
    executor = ScriptedExecutor(
        [SQLExecutionError("БД отклонила запрос (timeout)", SQLFailureClass.TIMEOUT)]
    )
    state = ExecuteSqlNode(executor).run({"sql": "SELECT 1"}, ctx=UNIT_CTX)
    assert "rows" not in state  # при ошибке результат не раскладывается
    assert state["error"]["class"] == "timeout"


def test_service_missing_sql_is_syntax_error_not_crash() -> None:
    executor = ScriptedExecutor([])
    state = ExecuteSqlNode(executor).run({}, ctx=UNIT_CTX)
    assert state["error"]["class"] == "syntax"


def test_service_input_state_not_mutated() -> None:
    executor = ScriptedExecutor([_rows(1)])
    original = {"sql": "SELECT 1"}
    ExecuteSqlNode(executor).run(original, ctx=UNIT_CTX)
    assert original == {"sql": "SELECT 1"}


# --- FSM-glue: make_execute_sql_node ---


def test_node_success_returns_partial_update() -> None:
    executor = ScriptedExecutor([_rows(2)])
    node = make_execute_sql_node(executor)
    updated = node(_base_state())
    assert updated["row_count"] == 2
    assert updated["error"] is None
    assert updated["route_flags"] == {}
    assert len(executor.requests) == 1  # probe не запускается


def test_node_builds_run_context_from_state() -> None:
    executor = ScriptedExecutor([_rows(1)])
    make_execute_sql_node(executor)(_base_state())
    assert executor.ctxs[0].user_id == "u1"
    assert executor.ctxs[0].trace_id == "tr-1"


def test_node_sql_error_routes_to_diagnose_context() -> None:
    executor = ScriptedExecutor(
        [SQLExecutionError("БД отклонила запрос (timeout)", SQLFailureClass.TIMEOUT)]
    )
    node = make_execute_sql_node(executor)
    updated = node(_base_state())
    assert updated["error"]["class"] == "timeout"
    assert len(executor.requests) == 1  # probe при ошибке не запускается


def test_node_missing_sql_is_fail_closed_defect() -> None:
    """SQL приходит только от validate_sql — отсутствие означает подмену каркаса."""
    executor = ScriptedExecutor([])
    tracer = RecordingTracer()
    updated = make_execute_sql_node(executor, tracer=tracer)(_base_state(sql=None))
    assert updated["route_flags"][EXECUTE_DEFECT_FLAG] is True
    assert executor.requests == []  # исполнения нет
    assert "execute.unavailable" in tracer.names()


def test_node_missing_identity_is_fail_closed_defect() -> None:
    executor = ScriptedExecutor([_rows(1)])
    tracer = RecordingTracer()
    updated = make_execute_sql_node(executor, tracer=tracer)(_base_state(user_id=""))
    assert updated["route_flags"][EXECUTE_DEFECT_FLAG] is True
    assert executor.requests == []


# --- probe COUNT (0 строк одиночного запроса) ---


def test_probe_lost_join_suspicion_when_all_tables_have_rows() -> None:
    """Подозрение на потерянный джойн: данные есть во всех таблицах, джойн пуст."""
    executor = ScriptedExecutor([_rows(0), _count(400), _count(120)])
    tracer = RecordingTracer()
    updated = make_execute_sql_node(executor, tracer=tracer)(_base_state())
    assert updated["route_flags"][PROBE_LOST_JOIN_FLAG] is True
    assert updated["error"]["class"] == "empty"
    assert "400" in updated["error"]["message"]
    probe_requests = executor.requests[1:]
    assert [r.sql for r in probe_requests] == [
        "SELECT COUNT(*) AS probe_count FROM orders",
        "SELECT COUNT(*) AS probe_count FROM customers",
    ]
    assert all(ctx.user_id == "u1" for ctx in executor.ctxs[1:])  # тот же канал (Ловушка 3)
    verdicts = [e.payload.get("verdict") for e in tracer.events if e.event == "execute.probe"]
    assert verdicts == ["lost_join"]


def test_probe_empty_joined_table_is_really_empty() -> None:
    """Связанная таблица пуста — валидный пустой ответ → compact_result (ruling R1)."""
    executor = ScriptedExecutor([_rows(0), _count(400), _count(0)])
    tracer = RecordingTracer()
    updated = make_execute_sql_node(executor, tracer=tracer)(_base_state())
    assert PROBE_LOST_JOIN_FLAG not in updated["route_flags"]
    assert updated["error"] is None  # пустой результат — не ошибка (Ловушка 2, S04 M2)
    verdicts = [e.payload.get("verdict") for e in tracer.events if e.event == "execute.probe"]
    assert verdicts == ["empty"]


def test_probe_empty_root_table_is_really_empty() -> None:
    executor = ScriptedExecutor([_rows(0), _count(0), _count(120)])
    updated = make_execute_sql_node(executor)(_base_state())
    assert PROBE_LOST_JOIN_FLAG not in updated["route_flags"]
    assert updated["error"] is None


def test_probe_failure_is_inconclusive_not_crash() -> None:
    """COUNT-проба сама упала — вердикта нет, валидный пустой ответ остаётся (S00-8)."""
    executor = ScriptedExecutor(
        [
            _rows(0),
            SQLExecutionError("проба не выполнилась", SQLFailureClass.CONNECTION),
        ]
    )
    tracer = RecordingTracer()
    updated = make_execute_sql_node(executor, tracer=tracer)(_base_state())
    assert PROBE_LOST_JOIN_FLAG not in updated["route_flags"]
    assert updated["error"] is None
    verdicts = [e.payload.get("verdict") for e in tracer.events if e.event == "execute.probe"]
    assert verdicts == ["empty"]


def test_probe_missing_skeleton_is_fail_closed_defect() -> None:
    """Проба строится по таблицам плана — отсутствие скелета означает подмену каркаса."""
    executor = ScriptedExecutor([_rows(0)])
    updated = make_execute_sql_node(executor)(_base_state(skeleton=None))
    assert updated["route_flags"][EXECUTE_DEFECT_FLAG] is True
    assert len(executor.requests) == 1  # пробы не было


def test_probe_skipped_in_step_loop_context() -> None:
    """Шаговый контур (S17) — probe-решение не его зона: флаги шагов старше."""
    executor = ScriptedExecutor([_rows(0)])
    updated = make_execute_sql_node(executor)(_base_state(route_flags={"step_done": True}))
    assert len(executor.requests) == 1
    assert PROBE_LOST_JOIN_FLAG not in updated["route_flags"]


# --- Маршруты routing.route_execute_sql (единственный источник, ruling R4) ---


def test_routing_defect_wins_over_error() -> None:
    from text_to_sql_agent.orchestration.routing import route_execute_sql

    state = {"error": {"class": "syntax"}, "route_flags": {EXECUTE_DEFECT_FLAG: True}}
    assert route_execute_sql(state) == "degrade"


def test_routing_probe_lost_join_to_diagnose() -> None:
    from text_to_sql_agent.orchestration.routing import route_execute_sql

    state = {"error": None, "rows": [], "route_flags": {PROBE_LOST_JOIN_FLAG: True}}
    assert route_execute_sql(state) == "diagnose_error"


def test_routing_really_empty_to_compact() -> None:
    from text_to_sql_agent.orchestration.routing import route_execute_sql

    state = {"error": None, "rows": [], "route_flags": {}}
    assert route_execute_sql(state) == "compact_result"


# --- Parity флагов: application не импортирует orchestration (прецедент S11) ---


def test_flag_names_parity_with_routing() -> None:
    from text_to_sql_agent.orchestration.routing import (
        FLAG_EXECUTE_DEFECT,
        FLAG_PROBE_LOST_JOIN,
    )

    assert PROBE_LOST_JOIN_FLAG == FLAG_PROBE_LOST_JOIN
    assert EXECUTE_DEFECT_FLAG == FLAG_EXECUTE_DEFECT
    assert set(_STEP_FLAG_NAMES) == {
        "step_done",
        "steps_pending",
        "step_empty_keyset",
        "step_keyset_overflow",
    }
