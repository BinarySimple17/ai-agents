"""Общие двойники юнит-тестов S13 — моки вместо БД (S00 правило 7, один файл = один компонент)."""

from text_to_sql_agent.application.ports.sql_executor import (
    QueryResult,
    SQLExecutionError,
)
from text_to_sql_agent.domain.entities import RunContext, Trace

UNIT_CTX = RunContext(trace_id="unit-s13", user_id="tester")


class FakeExecutor:
    """Двойник `SQLExecutor`: пишет запросы/контексты, отдаёт результат или ошибку."""

    def __init__(
        self,
        result: QueryResult | None = None,
        error: SQLExecutionError | None = None,
    ) -> None:
        self.result = result or QueryResult(columns=["id"], rows=[{"id": 1}], row_count=1)
        self.error = error
        self.requests = []
        self.ctxs = []

    def execute(self, request, *, ctx):  # сигнатура порта; типизация — в протоколе
        self.requests.append(request)
        self.ctxs.append(ctx)
        if self.error is not None:
            raise self.error
        return self.result


class RecordingTracer:
    """Двойник `Tracer`: собирает события — проверки последовательности и payload."""

    def __init__(self) -> None:
        self.events: list[Trace] = []

    def record(self, trace: Trace) -> None:
        self.events.append(trace)

    def names(self) -> list[str]:
        return [trace.event for trace in self.events]
