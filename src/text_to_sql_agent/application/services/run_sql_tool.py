"""Тул run_sql (ДЗ-02) — тонкая обёртка над `SQLExecutor` (ruling ARCH-GATE S13 Q3).

Единый канал SQL: тул не ходит в БД сам, только через порт (security §2.1) —
второй путь был бы дырой в рубеже 5. Аргументы вызова валидируются кодом
(deny-by-default): один обязательный строковый `sql`, лишние ключи отклоняются.
Наблюдение строит вызывающий контур (`apps.cli`): тул возвращает структуру,
которую контур маркирует как данные (security §3, S6). Ошибки исполнения
несёт `SQLExecutionError` — Observation собирает вызывающий контур.
"""

from collections.abc import Mapping
from typing import Any

from text_to_sql_agent.application.ports.llm_provider import ToolSpec
from text_to_sql_agent.application.ports.sql_executor import (
    ROW_LIMIT,
    QueryRequest,
    QueryResult,
    SQLExecutor,
)
from text_to_sql_agent.domain.entities import RunContext

TOOL_NAME = "run_sql"
SQL_ARGUMENT = "sql"

# ToolSpec в OpenAI-формате (ADR-008): тулы передаются параметром `tools`.
# Лимит в описании — канонный ROW_LIMIT порта (единый источник с адаптером).
_TOOL_SPEC: ToolSpec = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Выполнить ОДИН SELECT-запрос к демо-схеме (read-only роль). "
            "Возвращает columns, rows, row_count и флаг truncated "
            f"(результат срезан на {ROW_LIMIT} строк). Только SELECT — "
            "DML/DDL отклонит БД. Ошибка возвращается текстом: используй её "
            "для исправления SQL и повторного вызова."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                SQL_ARGUMENT: {
                    "type": "string",
                    "description": (
                        "Один SELECT-запрос на PostgreSQL. Свои LIMIT/OFFSET "
                        f"разрешены; итог всё равно капится {ROW_LIMIT} строками."
                    ),
                }
            },
            "required": [SQL_ARGUMENT],
            "additionalProperties": False,
        },
    },
}


class RunSqlTool:
    """Тул run_sql: валидация аргументов → исполнение через порт `SQLExecutor`."""

    def __init__(self, executor: SQLExecutor) -> None:
        self._executor = executor

    @property
    def tool_spec(self) -> ToolSpec:
        return _TOOL_SPEC

    def run(self, arguments: Mapping[str, Any], *, ctx: RunContext) -> QueryResult:
        """Исполнить SELECT из аргументов тулa; ошибки аргументов — ValueError."""
        sql = self._validate_arguments(arguments)
        return self._executor.execute(QueryRequest(sql=sql), ctx=ctx)

    @staticmethod
    def _validate_arguments(arguments: Mapping[str, Any]) -> str:
        unknown = sorted(set(arguments) - {SQL_ARGUMENT})
        if unknown:
            raise ValueError(
                f"{TOOL_NAME}: неизвестные аргументы {', '.join(unknown)} "
                f"(допустим только '{SQL_ARGUMENT}')"
            )
        sql = arguments.get(SQL_ARGUMENT)
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError(
                f"{TOOL_NAME}: аргумент '{SQL_ARGUMENT}' обязателен — непустая строка SELECT"
            )
        return sql.strip()
