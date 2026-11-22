"""Узел clarify для FSM (feat-008, S08) — glue сервис ↔ state (прецедент формы:
resolve_access_node S05, execute_sql_node S13).

Два визита одного узла (само-ребро clarify → clarify — routing, ruling R1/R2
ARCH-GATE S08):

- **visit 1 (классификация, LLM):** сервис возвращает вердикт — unambiguous →
  допущения в state, дальше plan_joins; ambiguous/insufficient_data →
  ``clarify_question`` в state (-routing по само-ребру возвращается в узел) и
  interrupt; fail → флаг БЕЗ вопроса → терминал clarification (честный отказ,
  S00-8).
- **visit 2 (interrupt-ветка, БЕЗ LLM — ruling R6):** вопрос уже в state →
  ``gate.ask`` (InterruptGate: interrupt + checkpointer, resume тем же thread)
  → ответ пользователя становится допущением ``{source: "user_answer"}`` →
  вопрос и флаг чистятся → план_joins. Пустой/whitespace-ответ — fail-closed
  отказ (терминал clarification), не молчаливое продолжение.

Каждый вход в классификацию учитывает фактический расход LLM в
``budget_used.llm_calls`` (гвард капов — routing.llm_guard); ₽-аккумулятор —
резидент адаптера, в state — BudgetGuard (S15, как в каркасе S04).
"""

from typing import Any
from uuid import uuid4

from text_to_sql_agent.application.ports.interrupt_gate import InterruptGate
from text_to_sql_agent.application.ports.tracer import (
    EVENT_CLARIFY_INTERRUPT,
    EVENT_CLARIFY_UNAVAILABLE,
    EVENT_CLARIFY_VERDICT,
    Tracer,
)
from text_to_sql_agent.application.services.clarify_service import (
    ClarifyOutcome,
    ClarifyService,
)
from text_to_sql_agent.domain.entities import Assumption, RunContext, Trace

# application не импортирует orchestration — имя флага дублируется из
# routing.FLAG_CLARIFICATION_NEEDED; совпадение строк гвардит тест
# (прецедент IDENTITY_FAILED_FLAG / FLAG_IDENTITY_FAILED S05)
CLARIFICATION_NEEDED_FLAG = "clarification_needed"

# Фиксированные сообщения отказов (S9-политика: пользовательский текст без
# деталей инфраструктуры — host/роль БД/имена моделей; ревью S05 Important-1)
_CLARIFY_UNAVAILABLE_MESSAGE = (
    "не удалось оценить однозначность вопроса — переформулируйте его подробнее"
)
_EMPTY_ANSWER_MESSAGE = "ответ на уточняющий вопрос пуст — запрос отклонён (fail-closed)"
_IDENTITY_MISSING_MESSAGE = "идентификация не пройдена — запрос отклонён (fail-closed)"

State = dict[str, Any]


def make_clarify_node(
    service: ClarifyService,
    gate: InterruptGate,
    *,
    tracer: Tracer | None = None,
) -> Any:
    """Фабрика узла clarify: сервис + InterruptGate (+ опциональный трейс)."""

    def node(state: State) -> dict[str, Any]:
        flags = dict(state.get("route_flags") or {})
        user_id = (state.get("user_id") or "").strip()
        pending_question = state.get("clarify_question")

        # --- interrupt-ветка (visit 2/resume): вопрос ждёт ответа — без LLM ---
        if pending_question:
            if tracer is not None:
                tracer.record(
                    Trace(
                        event=EVENT_CLARIFY_INTERRUPT,
                        payload={"user_id": user_id, "question": pending_question},
                    )
                )
            answer = gate.ask(pending_question).strip()
            if not answer:
                # fail-closed (S00-8): пустой ответ — честный отказ, не продолжение
                return {
                    "clarify_question": None,
                    "route_flags": {**flags, CLARIFICATION_NEEDED_FLAG: True},
                    "error": {"class": "clarify_empty_answer", "message": _EMPTY_ANSWER_MESSAGE},
                }
            assumptions = [
                *(state.get("assumptions") or []),
                Assumption(source="user_answer", text=answer),
            ]
            return {
                "assumptions": assumptions,
                "clarify_question": None,
                "route_flags": {**flags, CLARIFICATION_NEEDED_FLAG: False},
                "error": None,
            }

        # --- visit 1: классификация (LLM) ---
        if not user_id:
            # Defense-in-depth: до clarify граф проходит resolve_access (reject
            # пустой identity) — пустой user_id здесь означает подмену каркаса;
            # fail-closed, не RunContext-ValueError наружу графа
            return {
                "clarify_question": None,
                "route_flags": {**flags, CLARIFICATION_NEEDED_FLAG: True},
                "error": {"class": "identity_missing", "message": _IDENTITY_MISSING_MESSAGE},
            }
        ctx = RunContext(
            # trace_id ставит раннер (uuid прогона, корреляция ADR-010); fallback —
            # прямые вызовы узла в юнит-тестах мимо раннера
            trace_id=state.get("trace_id") or uuid4().hex,
            user_id=user_id,
        )
        result = service.clarify(
            state.get("question") or "",
            ctx=ctx,
            permissions=state.get("permissions") or {},
        )
        if tracer is not None:
            tracer.record(
                Trace(
                    event=EVENT_CLARIFY_VERDICT,
                    payload={
                        "user_id": user_id,
                        "outcome": result.outcome.value,
                        "assumptions": len(result.assumptions),
                    },
                )
            )
        budget = dict(state.get("budget_used") or {})
        budget["llm_calls"] = (budget.get("llm_calls") or 0) + result.llm_calls

        if result.outcome == ClarifyOutcome.UNAMBIGUOUS:
            return {
                "rewritten_question": None,  # переписывание вопроса — вне скоупа S08
                "assumptions": [*(state.get("assumptions") or []), *result.assumptions],
                "route_flags": {**flags, CLARIFICATION_NEEDED_FLAG: False},
                "error": None,
                "budget_used": budget,
            }
        if result.outcome == ClarifyOutcome.CLARIFY:
            return {
                "assumptions": [*(state.get("assumptions") or []), *result.assumptions],
                "clarify_question": result.question,
                "route_flags": {**flags, CLARIFICATION_NEEDED_FLAG: True},
                "error": None,
                "budget_used": budget,
            }
        # FAIL: деталь сбоя (LLM/retrieval, текст ошибки сервиса) — оператору в
        # трейс; пользователь — фиксированный текст (S9-политика)
        if tracer is not None and result.error:
            tracer.record(
                Trace(
                    event=EVENT_CLARIFY_UNAVAILABLE,
                    payload={"user_id": user_id, "detail": result.error},
                )
            )
        return {
            "clarify_question": None,
            "route_flags": {**flags, CLARIFICATION_NEEDED_FLAG: True},
            "error": {"class": "clarify_unavailable", "message": _CLARIFY_UNAVAILABLE_MESSAGE},
            "budget_used": budget,
        }

    return node
