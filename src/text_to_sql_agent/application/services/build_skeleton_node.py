"""Узел build_skeleton для FSM (feat-010, S10) — детерминированный код, БЕЗ LLM.

«JOIN-скелет: ключи, тип, обязательные предикаты» (hw-01 §2.1) — вся логика в
domain/skeleton.py (ruling R4 ARCH-GATE: VO + сборка в domain, единый источник
нормализации с валидатором S11); узел — glue: dict-state → JoinPlan → Skeleton →
state. План-сессия предлагал сервис-обёртку — не заведена (отклонение
зафиксировано в отчёте): BuildSkeletonService без зависимостей был бы прокладкой
(KISS).

Скелет может построиться только на PLAN-исходе plan_joins (единственное ребро в
узел — routing.route_plan_joins); отсутствие/брак плана здесь — дефект каркаса:
fail-closed БЕЗ LLM и retrieval, пользователь — фиксированный текст (S9), деталь
дефекта — оператору в трейс (skeleton.unavailable, прецедент clarify.unavailable);
флаг читает route_build_skeleton: FLAG_GENERATE_SQL_FAILED → degrade («генерация
невозможна», единый флаг с отказом генератора — ruling R2 ARCH-GATE S10).
"""

from typing import Any

from pydantic import ValidationError

from text_to_sql_agent.application.ports.tracer import (
    EVENT_SKELETON_UNAVAILABLE,
    Tracer,
)
from text_to_sql_agent.domain.entities import Trace
from text_to_sql_agent.domain.join_plan import JoinPlan
from text_to_sql_agent.domain.skeleton import build_skeleton

# application не импортирует orchestration — имя флага дублируется из
# routing.FLAG_GENERATE_SQL_FAILED; совпадение строк гвардит тест
# (прецедент CLARIFICATION_NEEDED_FLAG / FLAG_CLARIFICATION_NEEDED S08)
GENERATE_SQL_FAILED_FLAG = "generate_sql_failed"

# Фиксированное сообщение отказа (S9-политика: пользовательский текст без
# деталей инфраструктуры; деталь дефекта — оператору в трейс)
_SKELETON_DEFECT_MESSAGE = (
    "не удалось подготовить структуру запроса — переформулируйте вопрос или повторите позже"
)

State = dict[str, Any]


def make_build_skeleton_node(*, tracer: Tracer | None = None) -> Any:
    """Фабрика узла build_skeleton: чистая сборка кодом (+ опциональный трейс)."""

    def node(state: State) -> dict[str, Any]:
        raw_plan = state.get("join_plan")
        if not isinstance(raw_plan, dict):
            return _defect("join_plan отсутствует или не dict", tracer)
        try:
            plan = JoinPlan.model_validate(raw_plan)
        except ValidationError as exc:
            return _defect(f"join_plan не прошёл схему: {exc}", tracer)
        try:
            skeleton = build_skeleton(plan)
        except ValueError as exc:
            return _defect(str(exc), tracer)
        return {
            "skeleton": skeleton.model_dump(),
            "skeleton_hash": skeleton.skeleton_hash,
            "error": None,
        }

    return node


def _defect(detail: str, tracer: Tracer | None) -> dict[str, Any]:
    """Дефект каркаса (план не дошёл/не валиден): fail-closed в degrade-ветку.

    Флаг читает route_build_skeleton: «генерация невозможна» → degrade — отказ
    формулирует S15 бизнес-понятиями (ADR-005); деталь дефекта — оператору в
    трейс (S9), пользовательский текст фиксированный.
    """
    if tracer is not None:
        tracer.record(Trace(event=EVENT_SKELETON_UNAVAILABLE, payload={"detail": detail}))
    return {
        "skeleton": None,
        "skeleton_hash": None,
        "route_flags": {GENERATE_SQL_FAILED_FLAG: True},
        "error": {"class": "skeleton_defect", "message": _SKELETON_DEFECT_MESSAGE},
    }
