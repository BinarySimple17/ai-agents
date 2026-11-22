"""Узел degrade для FSM (feat-015, S15) — glue сервис ↔ state.

Код собирает типизированный вердикт-причину из кодовых каналов state (R8:
validation.outcome — S11, route_flags — routing, план/права) → сервис
формулирует отказ (mid + санитайзер R1) → узел пишет ``final_answer``
(тот же канал, что interpret_result — карта владения) и бюджетный учёт
(llm_calls + ₽-снапшот guard'а, ruling R6).

Приоритет причин (вершина = дефект каркаса): execute_defect → rights
(denied — сигнал безопасности, ADR-005) → no_path → generator → technical
(futile-разбор diagnose / лимит / дедлайн). SQL недоступной выборки в
final_answer не попадает (S9: текст SQL содержит чужие имена).

События (R7): degrade.verdict {cause, has_variant, used_llm} /
degrade.unavailable {reason} — причина отклонения текста модели (санитайзер/
шаблон) — оператору, пользователю — готовый текст отказа (S9).
"""

from typing import Any

from text_to_sql_agent.application.ports.budget_guard import BudgetGuard
from text_to_sql_agent.application.ports.tracer import (
    EVENT_DEGRADE_UNAVAILABLE,
    EVENT_DEGRADE_VERDICT,
    Tracer,
)
from text_to_sql_agent.application.services.degrade_service import DegradeService
from text_to_sql_agent.domain.degradation import (
    DegradationCase,
    DegradationCause,
    fallback_text,
)
from text_to_sql_agent.domain.entities import Trace

# application не импортирует orchestration — имена флагов дублируются из
# routing.FLAG_*; совпадение строк гвардит тест (прецедент S05/S08/S14)
_EXECUTE_DEFECT_FLAG = "execute_defect"
_NO_PATH_FLAG = "plan_joins_no_path"
_GENERATOR_FAILED_FLAG = "generate_sql_failed"
_DENIED_OUTCOME = "denied"

# Бизнес-формулировки ситуаций (код, без имён недоступных таблиц — S9; в промпт
# идут вместе с доступными темами, R8)
_SITUATIONS: dict[DegradationCause, str] = {
    DegradationCause.RIGHTS: "вопрос затрагивает данные, недоступные вашей роли",
    DegradationCause.NO_PATH: "данные, затронутые вопросом, пока не связаны в базе",
    DegradationCause.GENERATOR: "не удалось подготовить запрос к данным",
    DegradationCause.TECHNICAL: "запрос не выполнен из-за технической ошибки",
}

State = dict[str, Any]


def _compose_case(state: State) -> DegradationCase:
    """Вердикт-причина из кодовых каналов state (порядок — docstring узла)."""
    flags = state.get("route_flags") or {}
    permissions = state.get("permissions") or {}
    available = tuple(sorted(permissions.keys()))
    validation = state.get("validation") or {}
    if flags.get(_EXECUTE_DEFECT_FLAG):
        cause = DegradationCause.TECHNICAL
        forbidden: tuple[str, ...] = ()
    elif validation.get("outcome") == _DENIED_OUTCOME:
        cause = DegradationCause.RIGHTS
        # «чужие» имена — санитайзеру: план минус права (имена из колонки-level
        # denial не извлекаем — issues внутренний канал, не парсится)
        plan_tables = (state.get("join_plan") or {}).get("tables") or []
        forbidden = tuple(sorted(set(plan_tables) - set(permissions)))
    elif flags.get(_NO_PATH_FLAG):
        cause = DegradationCause.NO_PATH
        forbidden = ()
    elif flags.get(_GENERATOR_FAILED_FLAG):
        cause = DegradationCause.GENERATOR
        forbidden = ()
    else:
        cause = DegradationCause.TECHNICAL
        forbidden = ()
    return DegradationCase(
        cause=cause,
        situation=_SITUATIONS[cause],
        available_topics=available,
        forbidden_topics=forbidden,
    )


def make_degrade_node(
    service: DegradeService,
    *,
    budget_guard: BudgetGuard | None = None,
    tracer: Tracer | None = None,
) -> Any:
    """Фабрика узла degrade: вердикт-причина (код) + формулировка (mid/шаблон)."""

    def node(state: State) -> dict[str, Any]:
        case = _compose_case(state)
        result = service.formulate(case=case)

        budget = dict(state.get("budget_used") or {})
        budget["llm_calls"] = (budget.get("llm_calls") or 0) + result.llm_calls
        cost_rub = None
        if budget_guard is not None:
            cost_rub = round(budget_guard.spent_rub(), 6)
            budget["rub"] = cost_rub

        answer = {
            "text": result.text or fallback_text(case),
            "sql": None,  # SQL недоступной/несостоявшейся выборки не показывается (S9)
            "assumptions": [],
            "usage_notes": [],
            "row_count": state.get("row_count") or 0,
            "truncated": bool(state.get("truncated")),
            "approximate": bool(state.get("approximate")),
            "incomplete": bool(state.get("incomplete")),
            "cost_rub": cost_rub,
            "cause": case.cause.value,
            "has_variant": bool(case.available_topics),
        }
        update: dict[str, Any] = {"final_answer": answer, "budget_used": budget}

        if tracer is not None:
            if result.error is not None or not result.used_llm:
                tracer.record(
                    Trace(event=EVENT_DEGRADE_UNAVAILABLE, payload={"reason": result.error})
                )
            else:
                tracer.record(
                    Trace(
                        event=EVENT_DEGRADE_VERDICT,
                        payload={
                            "cause": case.cause.value,
                            "has_variant": answer["has_variant"],
                            "used_llm": result.used_llm,
                            "llm_calls": result.llm_calls,
                        },
                    )
                )
        return update

    return node
