"""Узел generate_sql для FSM (feat-010, S10) — glue сервис ↔ state (прецедент формы:
clarify_node S08, plan_joins_node S10).

Сервис наполняет скелет (тул fill_skeleton — 4 поля, финальный SQL собирает код,
ruling R1 ARCH-GATE) — узел кладёт его в state.sql (карта владения: sql —
generate_sql/generate_step_sql) и гасит флаг отказа; FAIL после внутреннего M1 →
FLAG_GENERATE_SQL_FAILED → узел degrade (ruling R2: носитель отказа генератора
при исчерпании/бюджете — канон degrade; терминал fail на диаграмме исходит только
из plan_joins). FSM-ретраи через diagnose_error — S14: контекст последней ошибки
придёт из state (сервис принимает last_sql/last_error параметром — ruling R7).

Каждый вход учитывает фактический расход LLM в budget_used.llm_calls (гвард
капов — routing.llm_guard); событие вердикта — узел, деталь сбоя — оператору в
трейс (S9), пользователю — фиксированный текст.
"""

from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from text_to_sql_agent.application.ports.tracer import (
    EVENT_GENERATE_SQL_UNAVAILABLE,
    EVENT_GENERATE_SQL_VERDICT,
    Tracer,
)
from text_to_sql_agent.application.services.generate_sql_service import (
    GenerateSqlOutcome,
    GenerateSqlService,
)
from text_to_sql_agent.domain.entities import RunContext, Trace
from text_to_sql_agent.domain.skeleton import Skeleton

# application не импортирует orchestration — имя флага дублируется из
# routing.FLAG_GENERATE_SQL_FAILED; совпадение строк гвардит тест (прецедент S08)
GENERATE_SQL_FAILED_FLAG = "generate_sql_failed"

# Фиксированные сообщения отказов (S9-политика: без деталей инфраструктуры)
_GENERATE_UNAVAILABLE_MESSAGE = (
    "не удалось подготовить запрос — попробуйте переформулировать вопрос или повторите позже"
)
_IDENTITY_MISSING_MESSAGE = "идентификация не пройдена — запрос отклонён (fail-closed)"
_EMPTY_QUESTION_MESSAGE = "вопрос пуст — сформулируйте его подробнее"
_EMPTY_SKELETON_MESSAGE = "структура запроса не подготовлена — повторите вопрос позже"

State = dict[str, Any]


def make_generate_sql_node(
    service: GenerateSqlService,
    *,
    tracer: Tracer | None = None,
) -> Any:
    """Фабрика узла generate_sql: сервис наполнения + опциональный трейс."""

    def node(state: State) -> dict[str, Any]:
        flags = dict(state.get("route_flags") or {})
        user_id = (state.get("user_id") or "").strip()

        if not user_id:
            # Defense-in-depth: identity проверена в resolve_access — пустой
            # user_id здесь означает подмену каркаса; fail-closed (S00-8)
            return _fail(flags, "identity_missing", _IDENTITY_MISSING_MESSAGE)
        question = (state.get("question") or "").strip()
        if not question:
            # Пустой вопрос дошёл бы до VectorStore.search (ValueError наружу
            # графа) — fail-closed до ног (прецедент clarify M-r6b)
            return _fail(flags, "generate_empty_question", _EMPTY_QUESTION_MESSAGE)
        raw_skeleton = state.get("skeleton")
        if not isinstance(raw_skeleton, dict):
            # Defense-in-depth: до generate_sql граф проходит build_skeleton
            # (дефект ловит его гвард) — отсутствие скелета означает подмену
            return _fail(flags, "skeleton_missing", _EMPTY_SKELETON_MESSAGE)
        try:
            skeleton = Skeleton.model_validate(raw_skeleton)
        except ValidationError as exc:
            return _fail(flags, "skeleton_invalid", _EMPTY_SKELETON_MESSAGE, detail=str(exc))

        ctx = RunContext(
            # trace_id ставит раннер (uuid прогона, корреляция ADR-010); fallback —
            # прямые вызовы узла в юнит-тестах мимо раннера
            trace_id=state.get("trace_id") or uuid4().hex,
            user_id=user_id,
        )
        result = service.generate(
            skeleton=skeleton,
            question=question,
            ctx=ctx,
            assumptions=state.get("assumptions") or [],
            permissions=state.get("permissions") or {},
        )
        if tracer is not None:
            tracer.record(
                Trace(
                    event=EVENT_GENERATE_SQL_VERDICT,
                    payload={
                        "user_id": user_id,
                        "outcome": result.outcome.value,
                        "llm_calls": result.llm_calls,
                        "escalated": result.escalated,
                        "tables": len(skeleton.tables),
                    },
                )
            )
        budget = dict(state.get("budget_used") or {})
        budget["llm_calls"] = (budget.get("llm_calls") or 0) + result.llm_calls

        if result.outcome == GenerateSqlOutcome.SQL:
            return {
                "sql": result.sql,
                "route_flags": {**flags, GENERATE_SQL_FAILED_FLAG: False},
                "error": None,
                "budget_used": budget,
            }
        # FAIL: исчерпание M1 / шлюз / retrieval — отказ генератора → degrade;
        # деталь сбоя — оператору в трейс (S9), пользователю — фиксированный текст
        if tracer is not None and result.error:
            tracer.record(
                Trace(
                    event=EVENT_GENERATE_SQL_UNAVAILABLE,
                    payload={"user_id": user_id, "detail": result.error},
                )
            )
        return _fail(flags, "generate_sql_unavailable", _GENERATE_UNAVAILABLE_MESSAGE, budget)

    return node


def _fail(
    flags: dict[str, Any],
    error_class: str,
    message: str,
    budget: dict[str, Any] | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Отказ генератора: FLAG_GENERATE_SQL_FAILED → degrade (ruling R2 ARCH-GATE)."""
    update: dict[str, Any] = {
        "sql": None,
        "route_flags": {**flags, GENERATE_SQL_FAILED_FLAG: True},
        "error": {"class": error_class, "message": message},
    }
    if detail is not None:
        update["error"]["detail"] = detail  # операторская деталь (пользователь видит message)
    if budget is not None:
        update["budget_used"] = budget
    return update
