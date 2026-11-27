"""Порт `SQLExecutor` — контракт канала исполнения SQL (S00; ADR-011 §3/§4.2).

DTO стыка — pydantic и живут здесь (прецедент: `llm_provider.py`; ARCH-GATE
S03 — pydantic в портах допустим). Дискриминатор `sql|spec`: PG-канал исполняет
kind="sql" (текст после AST-валидатора — S11), SAP-канал (ADR-011, после ДЗ-06) —
kind="spec" (формализованный IR — sqlglot для Open SQL неприменим).

Ошибки стыка типизированы: `SQLExecutionError` несёт `SQLFailureClass` — вход
ретраев политики M1 (V4) и Observation узла diagnose_error. Пустой результат —
валидный исход (S00 правило 8), не ошибка.
"""

from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator

from text_to_sql_agent.domain.entities import RunContext

QueryKind = Literal["sql", "spec"]

# Канонные пределы канала исполнения (S00 запреты) — единый источник для адаптера,
# тул-схемы и Observation: ROW_LIMIT — кап строк результата, STATEMENT_TIMEOUT_S —
# таймаут запроса в БД, CONNECT_TIMEOUT_S — таймаут установки соединения (правило 9:
# «чёрная дыра» вместо закрытого порта не должна зависать вне statement_timeout).
ROW_LIMIT = 1000
STATEMENT_TIMEOUT_S = 30
CONNECT_TIMEOUT_S = 10


class QueryRequest(BaseModel):
    """Запрос каналу исполнения: kind="sql" — текст SELECT (PG), kind="spec" — IR (SAP)."""

    kind: QueryKind = "sql"
    sql: str = ""
    spec: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> "QueryRequest":
        if self.kind == "sql" and not self.sql.strip():
            raise ValueError("kind='sql' требует непустой sql")
        if self.kind == "spec" and self.spec is None:
            raise ValueError("kind='spec' требует spec — формализованный IR (ADR-011 §3)")
        return self


class QueryResult(BaseModel):
    """Результат исполнения — контракт {columns, rows, row_count, truncated} (ADR-011 §3)."""

    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = Field(default=0, ge=0)
    truncated: bool = False  # срезано обёрткой LIMIT канала, не пользователем


class SQLFailureClass(StrEnum):
    """Класс сбоя исполнения — вход ретраев M1 и Observation diagnose_error (V4)."""

    SYNTAX = "syntax"
    # Несуществующий объект (колонка/таблица/функция): регенерация выборки
    # оправдана, в отличие от грамматики — диагностика строит подсказку из
    # фактических колонок мира (аудит 2026-11-23 A4, S33)
    SEMANTICS = "semantics"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    READ_ONLY = "read_only"
    CONNECTION = "connection"
    UNKNOWN = "unknown"


class SQLExecutionError(Exception):
    """Сбой канала исполнения: человекочитаемое сообщение + класс (не traceback — правило 8)."""

    def __init__(self, message: str, error_class: SQLFailureClass) -> None:
        super().__init__(message)
        self.error_class = error_class


@runtime_checkable
class SQLExecutor(Protocol):
    """Единственный канал SQL в данные (security §2.1): read-only роль, LIMIT, timeout."""

    def execute(self, request: QueryRequest, *, ctx: RunContext) -> QueryResult: ...
