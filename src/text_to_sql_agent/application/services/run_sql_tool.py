"""Тул run_sql — ЕДИНСТВЕННЫЙ рубеж исполнения тул-контура (S31/D-033 п.5, аудит A1/E1).

История: тонкая обёртка над `SQLExecutor` (S13, ruling ARCH-GATE Q3: тул не ходит
в БД сам, только через порт — security §2.1). Аудит 2026-11-23 A1/E1 [Высокая]:
тул исполнял SQL модели в обход AST-валидатора и прав — дыра второго контура
исполнения (MUST NOT AGENTS «пропускать валидацию» держался только на FSM).
S31 (вариант A, R1–R3 ARCH-GATE): каждый прогон тула проходит ТОТ ЖЕ рубеж, что
и FSM-ребро validate_sql — права по identity (PermissionStore.fetch_rights на
каждый прогон, ruling R4 S05) → `ValidateSqlService.validate_free` (общий ACL +
инвариант реестра: пара операндов JOIN — ребро активного снимка,
SchemaGraphStore.join_edges) → исполнитель получает ТОЛЬКО переписанный ok-текст
(LIMIT добавлен/скламплен, звёзды развёрнуты — ruling R1/R2 S11).

Отказы — типизированный `ToolGateError` со словарём классов FSM (R3, без новых
имён): ``validate_hallucination`` (форма/выдумка — ретрай оправдан, текст для
модели несёт issues: несуществующие имена секретов не раскрывают);
``acl_denied`` (вне прав — БЕЗ ретрая, ADR-005 §4 «degrade без retry», текст
фиксированный R2/S9 — имена чужих таблиц и issues только в трейс);
``rights_unavailable``/``graph_unavailable`` (инфраструктура — стоп, детали
оператору в трейс). Вердикт/дефекты — существующие события
``validate_sql.verdict``/``validate_sql.unavailable`` с полем ``contour``
("tool_loop") — наблюдаемость §2.5 observability.md.
"""

from collections.abc import Mapping
from typing import Any

from text_to_sql_agent.application.ports.llm_provider import ToolSpec
from text_to_sql_agent.application.ports.permission_store import (
    PermissionStore,
    RightsUnavailableError,
    UserRights,
)
from text_to_sql_agent.application.ports.schema_graph_store import (
    SchemaGraphError,
    SchemaGraphStore,
)
from text_to_sql_agent.application.ports.sql_executor import (
    ROW_LIMIT,
    QueryRequest,
    QueryResult,
    SQLExecutor,
)
from text_to_sql_agent.application.ports.tracer import (
    EVENT_VALIDATE_SQL_UNAVAILABLE,
    EVENT_VALIDATE_SQL_VERDICT,
    Tracer,
)
from text_to_sql_agent.application.services.validate_sql_service import (
    ValidateSqlResult,
    ValidateSqlService,
)
from text_to_sql_agent.domain.entities import RunContext, Trace
from text_to_sql_agent.domain.sql_validation import ValidationOutcome, ValidationVerdict

TOOL_NAME = "run_sql"
SQL_ARGUMENT = "sql"

# Классы отказа тул-контура — словарь FSM (R3 ARCH-GATE S31): validate_hallucination
# (ретрай оправдан) / acl_denied (стоп) / rights_unavailable, graph_unavailable (стоп)
GATE_CLASS_DENIED = "acl_denied"
GATE_CLASS_HALLUCINATION = "validate_hallucination"
GATE_CLASS_RIGHTS_UNAVAILABLE = "rights_unavailable"
GATE_CLASS_GRAPH_UNAVAILABLE = "graph_unavailable"

# Фиксированные тексты отказов (R2 ARCH-GATE S31, S9-политика: без имён таблиц и
# деталей инфраструктуры; для hallucination текст формируется из issues — их имена
# НЕ существуют в мире агента, раскрытия нет). Audience — модель Observation.
_DENIED_MESSAGE = "часть данных по этому вопросу вам недоступна"
_UNAVAILABLE_MESSAGE = "данные сейчас недоступны — повторите запрос позже"

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


class ToolGateError(Exception):
    """Отказ рубежа тул-контура — типизированный класс + текст для Observation.

    ``error_class`` — словарь FSM (см. константы GATE_CLASS_*). ``message`` —
    текст в Observation модели (denied/инфра — фиксированный R2/S9);
    ``detail`` — операторная деталь ТОЛЬКО для трейса (S9-прецедент
    validate_sql.unavailable), в Observation не попадает.
    """

    def __init__(self, error_class: str, message: str, *, detail: str = "") -> None:
        self.error_class = error_class
        self.message = message
        self.detail = detail
        super().__init__(message)


class RunSqlTool:
    """Тул run_sql за единым рубежом: права → validate_free → исполнитель.

    Зависимости — ТЕ ЖЕ экземпляры, что у FSM-ребра validate_sql (composition
    собирает одной сборкой): валидатор, PermissionStore, SchemaGraphStore.
    ``tracer`` — события валидации тул-контура (None — тесты без наблюдаемости).
    """

    def __init__(
        self,
        executor: SQLExecutor,
        validator: ValidateSqlService,
        permission_store: PermissionStore,
        graph_store: SchemaGraphStore,
        *,
        tracer: Tracer | None = None,
    ) -> None:
        self._executor = executor
        self._validator = validator
        self._permission_store = permission_store
        self._graph_store = graph_store
        self._tracer = tracer

    @property
    def tool_spec(self) -> ToolSpec:
        return _TOOL_SPEC

    def run(self, arguments: Mapping[str, Any], *, ctx: RunContext) -> QueryResult:
        """Исполнить SELECT из аргументов тула за рубежом; ошибки аргументов —
        ValueError, отказы рубежа — ToolGateError (см. модуль)."""
        sql = self._validate_arguments(arguments)
        validated = self._behind_gate(sql, ctx)
        return self._executor.execute(QueryRequest(sql=validated.sql), ctx=ctx)

    # --- Рубеж (каждый прогон — свежие права/каталог/рёбра) ---

    def _behind_gate(self, sql: str, ctx: RunContext) -> ValidateSqlResult:
        rights = self._fetch_rights(ctx)
        # БЕЗ коэрции значений: CSV-строка в правах обязана дойти до контракт-
        # чека валидатора (BL-014 ValueError), а не молча превратиться list'ом
        # в посимвольный мусор до проверки
        permissions = dict(rights.tables)
        data_catalog = self._fetch_catalog(ctx)
        join_edges = self._fetch_edges(ctx)
        try:
            result = self._validator.validate_free(
                sql,
                permissions=permissions,
                data_catalog=data_catalog,
                row_limit=ROW_LIMIT,
                join_edges=join_edges,
            )
        except ValueError as exc:
            # Контракт стыка (BL-014: CSV-строка в правах) — дефект wiring,
            # честный fail-closed, не «пропустили» (зеркало validate_sql_node)
            self._emit_unavailable(ctx, "permissions_contract", str(exc))
            raise ToolGateError(
                GATE_CLASS_RIGHTS_UNAVAILABLE, _UNAVAILABLE_MESSAGE, detail=str(exc)
            ) from None
        self._emit_verdict(ctx, result.verdict)
        if result.verdict.outcome is ValidationOutcome.OK:
            return result
        if result.verdict.outcome is ValidationOutcome.DENIED:
            # Существует, но вне прав: degrade без retry — модель не «уговаривает»
            # валидатор (ADR-005 §4); issues — только трейс (S9: имена чужих
            # таблиц не раскрывают существование данных)
            raise ToolGateError(
                GATE_CLASS_DENIED,
                _DENIED_MESSAGE,
                detail="; ".join(result.verdict.issues),
            )
        # Форма/выдумка: ретрай оправдан — текст для модели несёт issues
        # (несуществующие имена секретов не раскрывают)
        raise ToolGateError(GATE_CLASS_HALLUCINATION, "; ".join(result.verdict.issues))

    def _fetch_rights(self, ctx: RunContext) -> UserRights:
        try:
            rights = self._permission_store.fetch_rights(ctx.user_id)
        except RightsUnavailableError as exc:
            self._emit_unavailable(ctx, GATE_CLASS_RIGHTS_UNAVAILABLE, str(exc))
            raise ToolGateError(
                GATE_CLASS_RIGHTS_UNAVAILABLE, _UNAVAILABLE_MESSAGE, detail=str(exc)
            ) from None
        if not rights.user_known:
            # Defense-in-depth: verified-токен указал пользователя вне
            # agent_user_roles — отказ, не «пустой мир» (ruling R6 S05)
            detail = f"user {ctx.user_id!r} отсутствует в agent_user_roles"
            self._emit_unavailable(ctx, "identity_unknown", detail)
            raise ToolGateError(GATE_CLASS_DENIED, _DENIED_MESSAGE, detail=detail)
        return rights

    def _fetch_catalog(self, ctx: RunContext) -> frozenset[str]:
        try:
            return self._permission_store.data_catalog()
        except RightsUnavailableError as exc:
            self._emit_unavailable(ctx, "catalog_unavailable", str(exc))
            raise ToolGateError(
                GATE_CLASS_RIGHTS_UNAVAILABLE, _UNAVAILABLE_MESSAGE, detail=str(exc)
            ) from None

    def _fetch_edges(self, ctx: RunContext) -> frozenset[tuple[str, str]]:
        try:
            return self._graph_store.join_edges(ctx=ctx)
        except SchemaGraphError as exc:
            self._emit_unavailable(ctx, GATE_CLASS_GRAPH_UNAVAILABLE, str(exc))
            raise ToolGateError(
                GATE_CLASS_GRAPH_UNAVAILABLE, _UNAVAILABLE_MESSAGE, detail=str(exc)
            ) from None

    # --- Наблюдаемость (существующие события + contour, R3) ---

    def _emit_verdict(self, ctx: RunContext, verdict: ValidationVerdict) -> None:
        if self._tracer is None:
            return
        self._tracer.record(
            Trace(
                event=EVENT_VALIDATE_SQL_VERDICT,
                payload={
                    "contour": "tool_loop",
                    "user_id": ctx.user_id,
                    "outcome": verdict.outcome.value,
                    "issues_count": len(verdict.issues),
                    "enforced_limit": verdict.enforced_limit,
                    "issues": list(verdict.issues),
                },
            )
        )

    def _emit_unavailable(self, ctx: RunContext, class_: str, detail: str) -> None:
        if self._tracer is None:
            return
        self._tracer.record(
            Trace(
                event=EVENT_VALIDATE_SQL_UNAVAILABLE,
                payload={"contour": "tool_loop", "class": class_, "detail": detail},
            )
        )

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
