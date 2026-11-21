"""Узел-сервис FSM `execute_sql` (S00 стык 1): исполнение SQL каналом `SQLExecutor`.

Узел читает `sql` из state и пишет `rows`/`row_count`/`truncated`/`error`; переходы
назначает граф (ADR-002 — модель не рулит маршрутом), здесь — заготовка условного
ребра по FSM state-agent v1.5: SQL-ошибка → `diagnose_error`, 0 строк → `probe`
(probe COUNT — не отдельный узел), успех → `compact_result`. Подключение к графу —
V4. Пустой результат — валидный исход (S00 правило 8), не ошибка.
"""

from collections.abc import Mapping
from typing import Any, Literal

from text_to_sql_agent.application.ports.sql_executor import (
    QueryRequest,
    SQLExecutionError,
    SQLExecutor,
    SQLFailureClass,
)
from text_to_sql_agent.domain.entities import RunContext

RouteTo = Literal["compact_result", "probe", "diagnose_error"]

SQL_KEY = "sql"
ERROR_KEY = "error"
ROWS_KEY = "rows"
ROW_COUNT_KEY = "row_count"
TRUNCATED_KEY = "truncated"

ROUTE_ON_ERROR: Literal["diagnose_error"] = "diagnose_error"
ROUTE_ON_EMPTY: Literal["probe"] = "probe"
ROUTE_ON_SUCCESS: Literal["compact_result"] = "compact_result"

_MISSING_SQL_CLASS = SQLFailureClass.SYNTAX
_MISSING_SQL_MESSAGE = "в state нет SQL для исполнения"


def _drop_stale_result(state: dict[str, Any]) -> dict[str, Any]:
    """Старый результат из входного state противоречил бы записанной ошибке."""
    for key in (ROWS_KEY, ROW_COUNT_KEY, TRUNCATED_KEY):
        state.pop(key, None)
    return state


class ExecuteSqlNode:
    """Сервис узла execute_sql: исполнение + раскладка результата в state."""

    def __init__(self, executor: SQLExecutor) -> None:
        self._executor = executor

    def run(self, state: Mapping[str, Any], *, ctx: RunContext) -> dict[str, Any]:
        """Возвращает НОВЫЙ dict-state с результатом (state иммутабелен для LangGraph)."""
        updated = dict(state)
        sql = updated.get(SQL_KEY)
        if not isinstance(sql, str) or not sql.strip():
            updated[ERROR_KEY] = {
                "class": _MISSING_SQL_CLASS.value,
                "message": _MISSING_SQL_MESSAGE,
            }
            return _drop_stale_result(updated)
        try:
            result = self._executor.execute(QueryRequest(sql=sql), ctx=ctx)
        except SQLExecutionError as exc:
            updated[ERROR_KEY] = {"class": exc.error_class.value, "message": str(exc)}
            return _drop_stale_result(updated)
        updated[ERROR_KEY] = None
        updated[ROWS_KEY] = result.rows
        updated[ROW_COUNT_KEY] = result.row_count
        updated[TRUNCATED_KEY] = result.truncated
        return updated


def execute_sql_route(state: Mapping[str, Any]) -> RouteTo:
    """Заготовка условного ребра: маршрут — код (ADR-002); граф подключает в V4."""
    if state.get(ERROR_KEY):
        return ROUTE_ON_ERROR
    if not state.get(ROWS_KEY):
        return ROUTE_ON_EMPTY
    return ROUTE_ON_SUCCESS
