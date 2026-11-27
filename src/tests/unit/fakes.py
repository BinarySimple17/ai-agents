"""Общие двойники юнит-тестов S13 — моки вместо БД (S00 правило 7, один файл = один компонент)."""

from collections.abc import Mapping

from text_to_sql_agent.application.ports.permission_store import (
    RightsUnavailableError,
    UserRights,
)
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


class FakePermissionStore:
    """Двойник `PermissionStore` (S31): снапшот прав + каталог мира без БД.

    Дефолтный мир — tester видит orders/customers целиком ('*'): ok-путь тула.
    ``columns`` — фактические колонки для `columns_catalog` (S33 R2; дефолт —
    зеркальные к правам: '*' видит всё); GRANT-дрейф моделируется исключением
    таблицы из `columns` (адаптер отдаёт только видимые планировщику — ruling
    R1 S33). ``error`` — RightsUnavailableError у обоих чтений (fail-closed
    тула); ``known=False`` — verified-пользователь вне agent_user_roles (R6 S05).
    """

    def __init__(
        self,
        tables: Mapping[str, tuple[str, ...]] | None = None,
        *,
        catalog: frozenset[str] | None = None,
        columns: Mapping[str, tuple[str, ...]] | None = None,
        known: bool = True,
        error: Exception | None = None,
    ) -> None:
        self._tables = (
            dict(tables) if tables is not None else {"orders": ("*",), "customers": ("*",)}
        )
        self._catalog = catalog if catalog is not None else frozenset(self._tables)
        self._columns = (
            dict(columns)
            if columns is not None
            else {"orders": ("id", "customer_id", "total"), "customers": ("id", "name")}
        )
        self._known = known
        self._error = error
        self.calls: list[str] = []

    def settings_versions(self) -> dict[str, int]:
        return {"permissions": 1, "user_roles": 1}

    def fetch_rights(self, user_id: str) -> UserRights:
        self.calls.append(user_id)
        if self._error is not None:
            raise self._error
        return UserRights(
            user_id=user_id,
            user_known=self._known,
            roles=("tester-role",),
            tables=self._tables,
            versions=self.settings_versions(),
        )

    def data_catalog(self) -> frozenset[str]:
        if isinstance(self._error, RightsUnavailableError):
            raise self._error
        return self._catalog

    def columns_catalog(self, tables: frozenset[str]) -> dict[str, frozenset[str]]:
        if isinstance(self._error, RightsUnavailableError):
            raise self._error
        requested = {str(t).strip().lower() for t in tables}
        return {
            table: frozenset(cols)
            for table, cols in self._columns.items()
            if table in requested and cols
        }
