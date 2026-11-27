"""Узел validate_sql для FSM (feat-011, S11) — glue: чистый валидатор ↔ state
(прецедент формы: generate_sql_node S10, build_skeleton_node S10).

Главный рубеж безопасности (hw-01 §3.2, ADR-005 v1.2): БЕЗ LLM и IO — валидация
детерминированная (sqlglot-парсинг в сервисе, вердикт в domain — ruling R4
ARCH-GATE). Три исхода ведут маршрутом routing.route_validate_sql: ok →
check_cost; hallucination («не существует») → diagnose_error, retry оправдан;
denied («вне прав») → degrade БЕЗ retry — модель не «уговаривает» валидатор.
Вердикт — в state.validation (issues — внутренний контекст ретрая M1/diagnose;
пользовательский текст формулирует degrade бизнес-понятиями, БЕЗ имён таблиц —
S9). ``state.sql`` перезаписывается исполненным текстом: внешний LIMIT
добавлен/скламплен (ruling R1), литеральные звёзды развёрнуты в разрешённые
колонки (ruling R2) — к исполнителю уходит ТОЛЬКО проверенный текст.

Fail-closed дефекты (подмена каркаса: пустой identity/sql/скелет/план/хэш,
недоступный каталог, нарушение контракта прав BL-014) маршрутизируются как
denied → degrade без retry: дефект не чинится ретраем; деталь дефекта —
оператору в трейс (S9-прецедент skeleton.unavailable), пользователю —
фиксированный текст.
"""

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from text_to_sql_agent.application.ports.permission_store import RightsUnavailableError
from text_to_sql_agent.application.ports.sql_executor import ROW_LIMIT
from text_to_sql_agent.application.ports.tracer import (
    EVENT_VALIDATE_SQL_UNAVAILABLE,
    EVENT_VALIDATE_SQL_VERDICT,
    Tracer,
)
from text_to_sql_agent.application.services.s9_messages import ACL_DENIED_MESSAGE
from text_to_sql_agent.application.services.validate_sql_service import ValidateSqlService
from text_to_sql_agent.domain.entities import Trace
from text_to_sql_agent.domain.join_plan import JoinPlan
from text_to_sql_agent.domain.skeleton import Skeleton
from text_to_sql_agent.domain.sql_validation import (
    ColumnsCatalogMissError,
    ValidationOutcome,
    has_star_grant,
)

# application не импортирует orchestration — имена флагов дублируются из
# routing.FLAG_*; совпадение строк гвардит parity-тест (прецедент
# GENERATE_SQL_FAILED_FLAG S10 / CLARIFICATION_NEEDED_FLAG S08)
VALIDATE_HALLUCINATION_FLAG = "validate_hallucination"
ACL_DENIED_FLAG = "acl_denied"

# Фиксированные сообщения дефектов (S9-политика: без деталей инфраструктуры;
# детали — оператору в трейс через validate_sql.unavailable). Текст общий с
# execute_sql_node — один источник в s9_messages (ревью S33 M-4: формулировки
# не разъезжаются)
_DEFECT_MESSAGE = ACL_DENIED_MESSAGE
_IDENTITY_MISSING_MESSAGE = "идентификация не пройдена — запрос отклонён (fail-closed)"

State = dict[str, Any]


def make_validate_sql_node(
    service: ValidateSqlService,
    *,
    catalog_provider: Callable[[], frozenset[str] | set[str]],
    columns_catalog_provider: Callable[[frozenset[str]], dict[str, frozenset[str]]] | None = None,
    tracer: Tracer | None = None,
) -> Any:
    """Фабрика узла validate_sql: детерминированная валидация + маршрутизация.

    ``catalog_provider`` — чтение каталога мира (PermissionStore.data_catalog)
    на КАЖДЫЙ запрос (инвариант стыка 3: права/мир — не кэш узла).
    ``columns_catalog_provider`` — фактические колонки (PermissionStore.
    columns_catalog) по прецеденту того же стыка: чтение на КАЖДЫЙ визит,
    но ТОЛЬКО при '*'-правах (именованные колонки под '*' сверяются с
    каталогом — S33 A5; явные списки каталог не трогают). ``None`` при
    '*'-правах → ColumnsCatalogMissError домена → дефект (fail-closed
    wiring, R2 S33).
    """

    def node(state: State) -> dict[str, Any]:
        flags = dict(state.get("route_flags") or {})
        user_id = (state.get("user_id") or "").strip()
        if not user_id:
            # Defense-in-depth: identity проверена resolve_access — пустой
            # user_id здесь означает подмену каркаса (прецедент generate_sql_node)
            return _defect(flags, "identity_missing", _IDENTITY_MISSING_MESSAGE, tracer)

        sql = state.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            # Ребро generate_sql → validate_sql проходит только при готовом SQL —
            # отсутствие означает дефект каркаса, не пустую генерацию
            return _defect(flags, "sql_missing", _DEFECT_MESSAGE, tracer)
        skeleton = _validated_vo(state, "skeleton", Skeleton)
        if skeleton is None:
            return _defect(flags, "skeleton_invalid", _DEFECT_MESSAGE, tracer)
        join_plan = _validated_vo(state, "join_plan", JoinPlan)
        if join_plan is None:
            return _defect(flags, "join_plan_invalid", _DEFECT_MESSAGE, tracer)
        skeleton_hash = state.get("skeleton_hash")
        if not isinstance(skeleton_hash, str) or not skeleton_hash:
            # «Скелет не изменён» сверяется со state-хэшем — его отсутствие
            # означает подмену чекпоинта (build_skeleton всегда пишет хэш)
            return _defect(flags, "skeleton_hash_missing", _DEFECT_MESSAGE, tracer)
        permissions = state.get("permissions")
        if not isinstance(permissions, dict):
            return _defect(flags, "permissions_missing", _DEFECT_MESSAGE, tracer)
        try:
            data_catalog = catalog_provider()
        except RightsUnavailableError as exc:
            # fail-closed: каталог недоступен → отказ, не «пустой мир» (S00-8)
            return _defect(flags, "catalog_unavailable", _DEFECT_MESSAGE, tracer, detail=str(exc))
        # Каталог колонок — только при '*'-правах (S33 R2): ровно они поднимают
        # вопрос «существует ли колонка вообще»; соединение планировщика — одно
        # на визит валидации в '*'-сцене, вне её — ноль. Предикат гранта —
        # доменный has_star_grant (ревью re-M-4: одна семантика в одном месте)
        star_tables = frozenset(
            table for table in permissions if has_star_grant(permissions, table)
        )
        columns_catalog: dict[str, frozenset[str]] | None = None
        if star_tables and columns_catalog_provider is not None:
            try:
                columns_catalog = columns_catalog_provider(star_tables)
            except RightsUnavailableError as exc:
                return _defect(
                    flags, "catalog_unavailable", _DEFECT_MESSAGE, tracer, detail=str(exc)
                )
        try:
            result = service.validate(
                sql,
                skeleton=skeleton,
                join_plan=join_plan,
                skeleton_hash=skeleton_hash,
                permissions=permissions,
                data_catalog=data_catalog,
                row_limit=ROW_LIMIT,
                columns_catalog=columns_catalog,
            )
        except ColumnsCatalogMissError as exc:
            # R1 S33: GRANT-дрейф ('*'-право без записи в каталоге) — дефект
            # стыка, не нарушение контракта прав: отдельный класс для триажа
            # оператором (ревью S33 M-1), маршрут тот же — degrade без retry
            return _defect(flags, "columns_catalog_miss", _DEFECT_MESSAGE, tracer, detail=str(exc))
        except ValueError as exc:
            # Нарушение контракта стыка (BL-014: CSV-строка в правах) — дефект
            # wiring, честный fail-closed, не «пропустили»
            return _defect(flags, "permissions_contract", _DEFECT_MESSAGE, tracer, detail=str(exc))

        if tracer is not None:
            tracer.record(
                Trace(
                    event=EVENT_VALIDATE_SQL_VERDICT,
                    payload={
                        "user_id": user_id,
                        "outcome": result.verdict.outcome.value,
                        "issues_count": len(result.verdict.issues),
                        "enforced_limit": result.verdict.enforced_limit,
                    },
                )
            )
        return _route(flags, result)

    return node


def _validated_vo(state: State, key: str, vo_class: type):
    """VO из state (dict → pydantic); None — дефект каркаса (caller ветвит)."""
    raw = state.get(key)
    if not isinstance(raw, dict):
        return None
    try:
        return vo_class.model_validate(raw)
    except ValidationError:
        return None


def _route(flags: dict[str, Any], result: Any) -> dict[str, Any]:
    """Вердикт → state.validation + route_flags (носители routing.route_validate_sql)."""
    outcome = result.verdict.outcome
    validation = {
        "outcome": outcome.value,
        "issues": list(result.verdict.issues),
        "enforced_limit": result.verdict.enforced_limit,
    }
    if outcome is ValidationOutcome.OK:
        return {
            # Исполненный текст: LIMIT добавлен/скламплен (R1), звёзды развёрнуты
            # (R2) — исходный текст модели к исполнителю не идёт
            "sql": result.sql,
            "validation": validation,
            "route_flags": {
                **flags,
                VALIDATE_HALLUCINATION_FLAG: False,
                ACL_DENIED_FLAG: False,
            },
            "error": None,
        }
    denied = outcome is ValidationOutcome.DENIED
    # ревью S11 I1: пользовательский текст (state.error.message печатает CLI) —
    # ФИКСИРОВАННЫЙ бизнес-текст для обоих отказов (S9: без имён таблиц и деталей
    # инфраструктуры); тексты нарушений остаются во внутреннем validation.issues —
    # контекст ретрая M1/D-016 подключит S14 (сейчас diagnose_error — транзит)
    message = _DEFECT_MESSAGE
    return {
        "validation": validation,
        "route_flags": {
            **flags,
            ACL_DENIED_FLAG: denied,
            VALIDATE_HALLUCINATION_FLAG: not denied,
        },
        "error": {
            "class": "acl_denied" if denied else "validate_hallucination",
            "message": message,
        },
    }


def _defect(
    flags: dict[str, Any],
    error_class: str,
    message: str,
    tracer: Tracer | None,
    *,
    detail: str | None = None,
) -> dict[str, Any]:
    """Дефект каркаса/стыка: fail-closed в degrade-ветку (denied-маршрут).

    Ретрай дефект не чинит (скелет/план собирает код, каталог читает адаптер) —
    degrade без retry; деталь — оператору в трейс (S9), пользовательский текст
    фиксированный.
    """
    if tracer is not None and detail is not None:
        tracer.record(
            Trace(
                event=EVENT_VALIDATE_SQL_UNAVAILABLE,
                payload={"class": error_class, "detail": detail},
            )
        )
    return {
        "validation": {
            "outcome": ValidationOutcome.DENIED.value,
            "issues": [message],
            "enforced_limit": ROW_LIMIT,
        },
        "route_flags": {**flags, ACL_DENIED_FLAG: True, VALIDATE_HALLUCINATION_FLAG: False},
        "error": {"class": error_class, "message": message},
    }
