"""Узел plan_joins для FSM (feat-009 сервис → подключение S10) — glue сервис ↔ state
(прецедент формы: clarify_node S08, build_skeleton_node S10).

S09 построила PlanJoinsService (retrieval + граф + ранжирование + валидация), но
к FSM не подключала — подключение вехи V4 (S10, Task 4). Исходы сервиса → кодовые
флаги/поля state (маршрут читает routing — модель имя узла не возвращает):

- PLAN → ``join_plan`` (model_dump — dict-state) + чистые флаги зоны → build_skeleton;
- CLARIFY → ``clarify_question`` + FLAG_PLAN_JOINS_CLARIFY (одноразовый переход →
  само-ребро clarify → interrupt; правило S08-карты владения: планер обязан писать
  вопрос ВМЕСТЕ с флагом, иначе clarify выполнит повторную классификацию);
- NO_PATH → FLAG_PLAN_JOINS_NO_PATH → узел degrade (behavior feat-009: «нет
  активного пути» + заявка; отказ формулирует S15; ruling R2 ARCH-GATE S10);
- FAIL → FLAG_PLAN_JOINS_EXHAUSTED → terminal_fail (honest refusal; reuse
  флага исчерпания — routing.py: «флаг exhausted — решение узла»; error в state
  фиксирует сбой — карта владения «узлы, поймавшие сбой»).

Каждый вход учитывает фактический расход LLM в budget_used.llm_calls (прецедент
clarify_node); событие вердикта — узел, деталь сбоя — оператору в трейс (S9),
пользователю — фиксированный текст.
"""

from typing import Any
from uuid import uuid4

from text_to_sql_agent.application.ports.tracer import (
    EVENT_PLAN_JOINS_UNAVAILABLE,
    EVENT_PLAN_JOINS_VERDICT,
    Tracer,
)
from text_to_sql_agent.application.services.plan_joins_service import (
    PlanJoinsService,
    PlanOutcome,
)
from text_to_sql_agent.domain.entities import RunContext, Trace

# application не импортирует orchestration — имена флагов дублируются из
# routing.FLAG_*; совпадение строк гвардят тесты (прецедент S05/S08)
PLAN_JOINS_INVALID_FLAG = "plan_joins_invalid"
PLAN_JOINS_EXHAUSTED_FLAG = "plan_joins_exhausted"
PLAN_JOINS_NO_PATH_FLAG = "plan_joins_no_path"
PLAN_JOINS_CLARIFY_FLAG = "plan_joins_clarify"
T1_DECOMPOSE_FLAG = "t1_decompose"

# Фиксированные сообщения отказов (S9-политика: без деталей инфраструктуры —
# host/роль БД/имена моделей; деталь сбоя сервиса — в трейс)
_PLAN_UNAVAILABLE_MESSAGE = (
    "не удалось определить связанные данные для вопроса — попробуйте переформулировать его"
)
_IDENTITY_MISSING_MESSAGE = "идентификация не пройдена — запрос отклонён (fail-closed)"
_EMPTY_QUESTION_MESSAGE = "вопрос пуст — сформулируйте его подробнее"

State = dict[str, Any]

# Флаги зоны plan_joins: вердикт узла — ровно один из них (t1 — порог кода feat-017)
_ZONE_FLAGS: dict[str, bool] = {
    PLAN_JOINS_INVALID_FLAG: False,
    PLAN_JOINS_EXHAUSTED_FLAG: False,
    PLAN_JOINS_NO_PATH_FLAG: False,
    PLAN_JOINS_CLARIFY_FLAG: False,
    T1_DECOMPOSE_FLAG: False,
}


def make_plan_joins_node(
    service: PlanJoinsService,
    *,
    tracer: Tracer | None = None,
) -> Any:
    """Фабрика узла plan_joins: сервис S09 + опциональный трейс."""

    def node(state: State) -> dict[str, Any]:
        flags = dict(state.get("route_flags") or {})
        user_id = (state.get("user_id") or "").strip()

        if not user_id:
            # Defense-in-depth: до plan_joins граф проходит resolve_access (reject
            # пустой identity) — пустой user_id здесь означает подмену каркаса;
            # честный fail-closed отказ (флаг исчерпания — терминал fail)
            return _fail_verdict(flags, "identity_missing", _IDENTITY_MISSING_MESSAGE)
        question = (state.get("question") or "").strip()
        if not question:
            # Пустой вопрос дошёл бы до VectorStore.search (ValueError наружу
            # графа) — fail-closed до ног (прецедент clarify M-r6b)
            return _fail_verdict(flags, "plan_empty_question", _EMPTY_QUESTION_MESSAGE)

        ctx = RunContext(
            # trace_id ставит раннер (uuid прогона, корреляция ADR-010); fallback —
            # прямые вызовы узла в юнит-тестах мимо раннера
            trace_id=state.get("trace_id") or uuid4().hex,
            user_id=user_id,
        )
        result = service.plan(question, ctx=ctx, permissions=state.get("permissions") or {})
        if tracer is not None:
            tracer.record(
                Trace(
                    event=EVENT_PLAN_JOINS_VERDICT,
                    payload={
                        "user_id": user_id,
                        "outcome": result.outcome.value,
                        "graph_version": result.graph_version,
                        "candidate_tables": result.candidate_tables,
                        "hit_edge_ids": result.hit_edge_ids,
                    },
                )
            )
        budget = dict(state.get("budget_used") or {})
        budget["llm_calls"] = (budget.get("llm_calls") or 0) + result.llm_calls

        if result.outcome == PlanOutcome.PLAN:
            return {
                "join_plan": result.plan.model_dump() if result.plan else None,
                "route_flags": {**flags, **_ZONE_FLAGS},
                "error": None,
                "budget_used": budget,
            }
        if result.outcome == PlanOutcome.CLARIFY:
            # Правило карты владения S08: вопрос ВМЕСТЕ с флагом — иначе clarify
            # выполнит повторную классификацию (флаг — одноразовый переход)
            return {
                "clarify_question": result.clarifying_question,
                "route_flags": {**flags, **_ZONE_FLAGS, PLAN_JOINS_CLARIFY_FLAG: True},
                "error": None,
                "budget_used": budget,
            }
        if result.outcome == PlanOutcome.NO_PATH:
            # Behavior feat-009: «нет активного пути» → degrade + заявка (заявку
            # записал сервис); отказ формулирует S15, деталь — оператору (S9)
            if tracer is not None and result.error:
                tracer.record(
                    Trace(
                        event=EVENT_PLAN_JOINS_UNAVAILABLE,
                        payload={"user_id": user_id, "detail": result.error},
                    )
                )
            return {
                "route_flags": {**flags, **_ZONE_FLAGS, PLAN_JOINS_NO_PATH_FLAG: True},
                "error": {"class": "plan_no_path", "message": _PLAN_UNAVAILABLE_MESSAGE},
                "budget_used": budget,
            }
        # FAIL: исчерпание ретраев / сбой инфраструктуры — честный отказ
        # (terminal_fail); деталь — оператору в трейс, не пользователю (S9)
        if tracer is not None and result.error:
            tracer.record(
                Trace(
                    event=EVENT_PLAN_JOINS_UNAVAILABLE,
                    payload={"user_id": user_id, "detail": result.error},
                )
            )
        return _fail_verdict(flags, "plan_failed", _PLAN_UNAVAILABLE_MESSAGE, budget)

    return node


def _fail_verdict(
    flags: dict[str, Any],
    error_class: str,
    message: str,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Честный отказ узла: флаг исчерпания → terminal_fail (routing)."""
    update: dict[str, Any] = {
        "route_flags": {**flags, **_ZONE_FLAGS, PLAN_JOINS_EXHAUSTED_FLAG: True},
        "error": {"class": error_class, "message": message},
    }
    if budget is not None:
        update["budget_used"] = budget
    return update
