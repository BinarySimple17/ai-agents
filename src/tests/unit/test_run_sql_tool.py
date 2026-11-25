"""Юнит-тесты тула run_sql (S13 Task 3 + S31 Task 2): аргументы, делегирование
порту, рубеж — права → validate_free → исполнитель (аудит A1/E1, R1–R3)."""

import pytest

from tests.unit.fakes import (
    UNIT_CTX,
    FakeExecutor,
    FakePermissionStore,
    FakeSchemaGraphStore,
)
from text_to_sql_agent.application.ports.permission_store import RightsUnavailableError
from text_to_sql_agent.application.ports.schema_graph_store import SchemaGraphError
from text_to_sql_agent.application.ports.sql_executor import (
    SQLExecutionError,
    SQLFailureClass,
)
from text_to_sql_agent.application.services.run_sql_tool import TOOL_NAME, RunSqlTool, ToolGateError
from text_to_sql_agent.application.services.validate_sql_service import ValidateSqlService

# Мир юнит-тестов: tester видит orders/customers; каталог знает и employees
# (для изоляции «существует, но вне прав» от «не существует в мире»)
WORLD_RIGHTS = {"orders": ("*",), "customers": ("*",)}
WORLD_CATALOG = frozenset({"orders", "customers", "employees", "bonuses"})


@pytest.fixture()
def executor() -> FakeExecutor:
    return FakeExecutor()


@pytest.fixture()
def tool(executor: FakeExecutor) -> RunSqlTool:
    return RunSqlTool(
        executor,
        ValidateSqlService(),
        FakePermissionStore(WORLD_RIGHTS, catalog=WORLD_CATALOG),
        FakeSchemaGraphStore(),
    )


def test_tool_spec_is_openai_format(tool: RunSqlTool) -> None:
    function = tool.tool_spec["function"]
    assert function["name"] == TOOL_NAME
    assert function["parameters"]["required"] == ["sql"]
    assert function["parameters"]["additionalProperties"] is False
    assert "SELECT" in function["description"]


def test_run_validates_then_executes_rewritten_sql(
    tool: RunSqlTool, executor: FakeExecutor
) -> None:
    # ok-вердикт: исполнитель получает ТОЛЬКО переписанный текст (R1 S11:
    # внешний LIMIT добавлен — исходный текст модели до БД не идёт)
    result = tool.run({"sql": "  SELECT 1  "}, ctx=UNIT_CTX)
    assert result.row_count == 1
    assert executor.requests[0].sql == "SELECT 1 LIMIT 1000"
    assert executor.requests[0].kind == "sql"
    assert executor.ctxs[0] is UNIT_CTX


def test_unknown_argument_rejected(tool: RunSqlTool) -> None:
    with pytest.raises(ValueError, match="неизвестные аргументы"):
        tool.run({"sql": "SELECT 1", "format": "json"}, ctx=UNIT_CTX)


def test_empty_sql_rejected(tool: RunSqlTool) -> None:
    with pytest.raises(ValueError, match="обязателен"):
        tool.run({"sql": "   "}, ctx=UNIT_CTX)


def test_non_string_sql_rejected(tool: RunSqlTool) -> None:
    with pytest.raises(ValueError):
        tool.run({"sql": 42}, ctx=UNIT_CTX)


def test_execution_error_passes_through(tool: RunSqlTool, executor: FakeExecutor) -> None:
    executor.error = SQLExecutionError("БД отклонила запрос", SQLFailureClass.TIMEOUT)
    with pytest.raises(SQLExecutionError):
        tool.run({"sql": "SELECT 1"}, ctx=UNIT_CTX)


# --- Рубеж (S31 Task 2, аудит A1/E1) ---


def test_sql_outside_rights_never_reaches_executor(
    executor: FakeExecutor,
) -> None:
    # РЕГРЕССИЯ Critical аудита A1/E1: SQL по чужой таблице не доходит до БД —
    # до S31 тул исполнял любой SQL мимо валидатора и прав (дыра второго
    # контура исполнения); тест падает на до-фиксовом коде (bite, worktree)
    tool = RunSqlTool(
        executor,
        ValidateSqlService(),
        FakePermissionStore(WORLD_RIGHTS, catalog=WORLD_CATALOG),
        FakeSchemaGraphStore(),
    )
    with pytest.raises(ToolGateError) as excinfo:
        tool.run({"sql": "SELECT salary FROM employees"}, ctx=UNIT_CTX)
    assert excinfo.value.error_class == "acl_denied"
    assert executor.requests == []  # исполнитель (шпион) не вызывался
    # R2/S9: фиксированный текст без имён чужих таблиц
    assert "employees" not in str(excinfo.value)
    assert "salary" not in str(excinfo.value)
    # issues — только в detail для трейса
    assert "employees" in excinfo.value.detail


def test_invented_join_never_reaches_executor(executor: FakeExecutor) -> None:
    # R1: обе таблицы разрешены, но связи в реестре нет — «джойн придуман»
    tool = RunSqlTool(
        executor,
        ValidateSqlService(),
        FakePermissionStore(WORLD_RIGHTS, catalog=WORLD_CATALOG),
        FakeSchemaGraphStore(frozenset()),  # пустой снимок: любое соединение — выдумка
    )
    with pytest.raises(ToolGateError) as excinfo:
        tool.run(
            {"sql": "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id"},
            ctx=UNIT_CTX,
        )
    assert excinfo.value.error_class == "validate_hallucination"
    assert "не существует в реестре" in excinfo.value.message  # ретрай оправдан
    assert executor.requests == []


def test_hallucination_message_carries_issues_for_retry(
    executor: FakeExecutor,
) -> None:
    tool = RunSqlTool(
        executor,
        ValidateSqlService(),
        FakePermissionStore(WORLD_RIGHTS, catalog=WORLD_CATALOG),
        FakeSchemaGraphStore(),
    )
    with pytest.raises(ToolGateError) as excinfo:
        tool.run({"sql": "SELECT pg_sleep(1)"}, ctx=UNIT_CTX)
    assert excinfo.value.error_class == "validate_hallucination"
    assert "pg_sleep" in excinfo.value.message  # несуществующие имена — не секреты


def test_rights_unavailable_fail_closed(executor: FakeExecutor) -> None:
    # «пустой мир молча» запрещён (S00-8): сбой чтения прав — отказ, не пустые права
    tool = RunSqlTool(
        executor,
        ValidateSqlService(),
        FakePermissionStore(error=RightsUnavailableError("db down")),
        FakeSchemaGraphStore(),
    )
    with pytest.raises(ToolGateError) as excinfo:
        tool.run({"sql": "SELECT 1"}, ctx=UNIT_CTX)
    assert excinfo.value.error_class == "rights_unavailable"
    assert executor.requests == []


def test_unknown_user_fail_closed(executor: FakeExecutor) -> None:
    # verified-пользователь вне agent_user_roles — отказ, не «пустой мир» (R6 S05)
    tool = RunSqlTool(
        executor,
        ValidateSqlService(),
        FakePermissionStore(WORLD_RIGHTS, catalog=WORLD_CATALOG, known=False),
        FakeSchemaGraphStore(),
    )
    with pytest.raises(ToolGateError) as excinfo:
        tool.run({"sql": "SELECT 1"}, ctx=UNIT_CTX)
    assert excinfo.value.error_class == "acl_denied"
    assert executor.requests == []


def test_graph_unavailable_fail_closed(executor: FakeExecutor) -> None:
    # R1: рёбра недоступны — инвариант реестра не выключается при сбое графа
    tool = RunSqlTool(
        executor,
        ValidateSqlService(),
        FakePermissionStore(WORLD_RIGHTS, catalog=WORLD_CATALOG),
        FakeSchemaGraphStore(error=SchemaGraphError("neo4j down")),
    )
    with pytest.raises(ToolGateError) as excinfo:
        tool.run({"sql": "SELECT 1"}, ctx=UNIT_CTX)
    assert excinfo.value.error_class == "graph_unavailable"
    assert executor.requests == []


def test_permissions_contract_defect_fail_closed(executor: FakeExecutor) -> None:
    # BL-014: CSV-строка в правах — дефект стыка, честный fail-closed отказ
    # (зеркало validate_sql_node.permissions_contract), не тихое исполнение
    store = FakePermissionStore({"orders": "id,total"}, catalog=WORLD_CATALOG)  # type: ignore[dict-item]
    tool = RunSqlTool(executor, ValidateSqlService(), store, FakeSchemaGraphStore())
    with pytest.raises(ToolGateError) as excinfo:
        tool.run({"sql": "SELECT id FROM orders"}, ctx=UNIT_CTX)
    assert excinfo.value.error_class == "rights_unavailable"
    assert "BL-014" in excinfo.value.detail
    assert executor.requests == []


def test_verdict_event_with_tool_loop_contour(executor: FakeExecutor) -> None:
    # R3: существующее событие validate_sql.verdict с полем contour="tool_loop"
    from tests.unit.fakes import RecordingTracer

    tracer = RecordingTracer()
    tool = RunSqlTool(
        executor,
        ValidateSqlService(),
        FakePermissionStore(WORLD_RIGHTS, catalog=WORLD_CATALOG),
        FakeSchemaGraphStore(),
        tracer=tracer,
    )
    tool.run({"sql": "SELECT 1"}, ctx=UNIT_CTX)
    verdict = next(e for e in tracer.events if e.event == "validate_sql.verdict")
    assert verdict.payload["contour"] == "tool_loop"
    assert verdict.payload["outcome"] == "ok"
    assert verdict.payload["user_id"] == UNIT_CTX.user_id
    assert "issues" in verdict.payload
