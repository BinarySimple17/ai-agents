"""Юнит-тесты узла execute_sql (S13 Task 3): раскладка в state + заготовка ребра."""

import pytest

from tests.unit.fakes import UNIT_CTX, FakeExecutor
from text_to_sql_agent.application.ports.sql_executor import (
    SQLExecutionError,
    SQLFailureClass,
)
from text_to_sql_agent.application.services.execute_sql_node import (
    ExecuteSqlNode,
    execute_sql_route,
)


@pytest.fixture()
def executor() -> FakeExecutor:
    return FakeExecutor()


@pytest.fixture()
def node(executor: FakeExecutor) -> ExecuteSqlNode:
    return ExecuteSqlNode(executor)


def test_success_lays_result_into_state(node: ExecuteSqlNode) -> None:
    state = node.run({"sql": "SELECT 1"}, ctx=UNIT_CTX)
    assert state["error"] is None
    assert state["row_count"] == 1
    assert state["rows"] == [{"id": 1}]
    assert state["truncated"] is False


def test_error_is_structured_not_traceback(node: ExecuteSqlNode) -> None:
    executor = FakeExecutor(
        error=SQLExecutionError("БД отклонила запрос (timeout)", SQLFailureClass.TIMEOUT)
    )
    state = ExecuteSqlNode(executor).run({"sql": "SELECT 1"}, ctx=UNIT_CTX)
    assert "rows" not in state  # при ошибке результат не раскладывается
    assert state["error"]["class"] == "timeout"
    assert "timeout" in state["error"]["message"]


def test_missing_sql_is_syntax_error_not_crash(node: ExecuteSqlNode) -> None:
    state = node.run({}, ctx=UNIT_CTX)
    assert state["error"]["class"] == "syntax"


def test_input_state_not_mutated(node: ExecuteSqlNode) -> None:
    original = {"sql": "SELECT 1"}
    node.run(original, ctx=UNIT_CTX)
    assert original == {"sql": "SELECT 1"}


def test_route_error_to_diagnose() -> None:
    assert execute_sql_route({"error": {"class": "timeout"}}) == "diagnose_error"


def test_route_empty_rows_to_probe() -> None:
    assert execute_sql_route({"error": None, "rows": []}) == "probe"


def test_route_success_to_compact() -> None:
    assert execute_sql_route({"error": None, "rows": [{"id": 1}]}) == "compact_result"
