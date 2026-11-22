"""Узел interpret_result для FSM (feat-015, S15) — glue сервис ↔ state.

Сервис объясняет компакт (strong + тул report_answer); узел собирает
финальный payload ответа (``final_answer`` — новое поле AgentState, карта
владения: interpret_result/degrade пишут, CLI/Streamlit читают) и ведёт
бюджетный учёт: llm_calls += result.llm_calls, ``rub`` — снапшот
budget_guard.spent_rub() (ruling R6 ARCH-GATE S15 — ₽-половина llm_guard
становится живой; без guard'а cost_rub=None — честно, 0 ₽ не маскируется).

INCOMPLETE-исход (M1/шлюз/бюджет — Ловушка 5: «остатка нет → отказ без
вызова» ловит llm_guard на ребре и исключение адаптера) → state.incomplete
+ шаблонный текст с row_count (данные получены — честно; детали сбоя —
S9-трейс, не пользователю). События вердикта — семья R5 S14 (R7): payload
факты, БЕЗ текста ответа и строк данных.
"""

from typing import Any

from text_to_sql_agent.application.ports.budget_guard import BudgetGuard
from text_to_sql_agent.application.ports.tracer import (
    EVENT_INTERPRET_UNAVAILABLE,
    EVENT_INTERPRET_VERDICT,
    Tracer,
)
from text_to_sql_agent.application.services.interpret_result_service import (
    InterpretOutcome,
    InterpretService,
)
from text_to_sql_agent.domain.entities import Trace

# Ключ payload финального ответа: текст + SQL + допущения + флаги + стоимость
# (критерий приёмки feat-015); degrade пишет тот же канал (карта владения)
FINAL_ANSWER_KEY = "final_answer"

# Шаблон честного отказа объяснения (S9: фиксированный текст, row_count — факт
# данных, детали инфраструктуры — только в трейс; Ловушка 4: причин сбоя
# интерпретатор не «объясняет» — только свои входы)
_INCOMPLETE_TEMPLATE = (
    "Данные получены ({row_count} строк), но объяснение к ним недоступно — "
    "ответ неполный. Повторите вопрос позже."
)

# application не импортирует orchestration — имя флага не нужно: узел пишет
# содержательное поле state.incomplete (карта владения: BudgetGuard/interpret — S15)

State = dict[str, Any]


def make_interpret_result_node(
    service: InterpretService,
    *,
    budget_guard: BudgetGuard | None = None,
    tracer: Tracer | None = None,
) -> Any:
    """Фабрика узла interpret_result: сервис + optional guard снапшота ₽ + трейс."""

    def node(state: State) -> dict[str, Any]:
        compact = state.get("compact") if isinstance(state.get("compact"), dict) else {}
        row_count = int(compact.get("row_count") or 0)
        truncated = bool(state.get("truncated"))
        approximate = bool(state.get("approximate"))
        incomplete_before = bool(state.get("incomplete"))
        assumptions = [
            item.get("text", "")
            for item in (state.get("assumptions") or [])
            if isinstance(item, dict)
        ]
        usage_notes = list((state.get("join_plan") or {}).get("assumptions") or [])
        cost_rub = round(budget_guard.spent_rub(), 6) if budget_guard is not None else None

        result = service.interpret(
            question=(state.get("question") or "").strip(),
            sql=state.get("sql"),
            compact=compact,
            assumptions=assumptions,
            usage_notes=usage_notes,
            cost_rub=cost_rub,
            truncated=truncated,
            approximate=approximate,
            incomplete=incomplete_before,
        )

        budget = dict(state.get("budget_used") or {})
        budget["llm_calls"] = (budget.get("llm_calls") or 0) + result.llm_calls
        if budget_guard is not None:
            # Снапшот ПОСЛЕ вызова сервиса — факт прогона по usage (R6), не 0 ₽
            budget["rub"] = round(budget_guard.spent_rub(), 6)

        incomplete = (
            incomplete_before
            or result.incomplete
            or (result.outcome is InterpretOutcome.INCOMPLETE)
        )
        text = result.text
        if result.outcome is InterpretOutcome.INCOMPLETE:
            text = _INCOMPLETE_TEMPLATE.format(row_count=row_count)
        answer = {
            "text": text,
            "sql": state.get("sql"),
            "assumptions": assumptions,
            "usage_notes": usage_notes,
            "row_count": row_count,
            "truncated": truncated,
            "approximate": approximate,
            "incomplete": incomplete,
            "cost_rub": cost_rub,
        }
        update: dict[str, Any] = {
            FINAL_ANSWER_KEY: answer,
            "budget_used": budget,
        }
        if incomplete != incomplete_before:
            update["incomplete"] = incomplete

        if tracer is not None:
            if result.outcome is InterpretOutcome.INCOMPLETE:
                tracer.record(
                    Trace(
                        event=EVENT_INTERPRET_UNAVAILABLE,
                        payload={"reason": result.error},
                    )
                )
            else:
                tracer.record(
                    Trace(
                        event=EVENT_INTERPRET_VERDICT,
                        payload={
                            "outcome": result.outcome.value,
                            "incomplete": incomplete,
                            "llm_calls": result.llm_calls,
                            "row_count": row_count,
                            "cost_rub": cost_rub,
                            "truncated": truncated,
                            "approximate": approximate,
                        },
                    )
                )
        return update

    return node
