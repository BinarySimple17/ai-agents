"""Юнит-тесты тулa run_sql (S13 Task 3): валидация аргументов, делегирование порту."""

import pytest

from tests.unit.fakes import UNIT_CTX, FakeExecutor
from text_to_sql_agent.application.ports.sql_executor import (
    SQLExecutionError,
    SQLFailureClass,
)
from text_to_sql_agent.application.services.run_sql_tool import TOOL_NAME, RunSqlTool


@pytest.fixture()
def executor() -> FakeExecutor:
    return FakeExecutor()


@pytest.fixture()
def tool(executor: FakeExecutor) -> RunSqlTool:
    return RunSqlTool(executor)


def test_tool_spec_is_openai_format(tool: RunSqlTool) -> None:
    function = tool.tool_spec["function"]
    assert function["name"] == TOOL_NAME
    assert function["parameters"]["required"] == ["sql"]
    assert function["parameters"]["additionalProperties"] is False
    assert "SELECT" in function["description"]


def test_run_executes_through_port(tool: RunSqlTool, executor: FakeExecutor) -> None:
    result = tool.run({"sql": "  SELECT 1  "}, ctx=UNIT_CTX)
    assert result.row_count == 1
    assert executor.requests[0].sql == "SELECT 1"
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
