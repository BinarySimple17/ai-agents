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
    GenerateFailKind,
    GenerateSqlOutcome,
    GenerateSqlService,
)
from text_to_sql_agent.domain.entities import RunContext, Trace
from text_to_sql_agent.domain.skeleton import Skeleton

# application не импортирует orchestration — имя флага дублируется из
# routing.FLAG_*; совпадение строк гвардит тест (прецедент S05/S08)
GENERATE_SQL_FAILED_FLAG = "generate_sql_failed"
# BL-016 (ruling R3 ARCH-GATE S14): generation-fail на FSM-ретрай-визите —
# ретрай оправдан, узел ведёт ребро generate_sql → retry_generate_sql (гейт) →
# визит 3 (эскалация strong с бюджет-проверкой в сервисе). Только на
# ретрай-визите: первый визит уже потратил внутренние попытки 1→2→3 (M1)
GENERATE_SQL_RETRYABLE_FLAG = "generate_sql_retryable"
# Гейт ретрая пишет имя ретрауемого узла в route_flags (S00 стык 2 — контракт
# стыка, не orchestration-константа): чтение из state, дублирование имени —
# гвардится parity-тестом
RETRY_TARGET_FLAG = "retry_target"

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
        except ValidationError:
            # Деталь ValidationError — в трейс не идёт (defect-ветка, класс ошибки
            # достаточен для триажа); контракт state.error — {class, message}
            return _fail(flags, "skeleton_invalid", _EMPTY_SKELETON_MESSAGE)

        ctx = RunContext(
            # trace_id ставит раннер (uuid прогона, корреляция ADR-010); fallback —
            # прямые вызовы узла в юнит-тестах мимо раннера
            trace_id=state.get("trace_id") or uuid4().hex,
            user_id=user_id,
        )
        # FSM-ретрай (ревью S10 I3): контекст последней попытки — из state
        # (S00 стык 2: счётчик/цель ретрая пишет код-гейт retry_generate_sql —
        # S14 активирует рёбра diagnose_error → generate_sql; попытка 1 —
        # состояние «вне ретрая»). Сервис нумерует попытки непрерывно:
        # визит FSM-ретрая стартует с попытки 2 (та же роль + ошибка) или 3
        # (эскалация) — суммарно ≤ 3 на узел, не 3×3.
        retry_target = flags.get(RETRY_TARGET_FLAG)
        retry_count = state.get("retry_count") or 0
        on_fsm_retry = retry_target == "generate_sql"
        start_attempt = retry_count + 1 if on_fsm_retry else 1
        # Контекст ретрая — только на FSM-ретрае (ревью minor): затерявшийся
        # error другого узла не должен превращаться в «Ошибка предыдущей попытки»
        # S14 (D-016): разбор ошибки — последний SQL + внутренний факт ошибки +
        # подсказка диагностики (state.diagnosis пишет diagnose_error — S9: hint
        # внутренний). S33: факт — error["detail"] (сырой текст БД — модель
        # видит фактические имена, ловушка 2), фиксированный message —
        # пользовательский текст, модели его больше не отдаём
        last_error = None
        if on_fsm_retry:
            error_dict = state.get("error") or {}
            raw_error = error_dict.get("detail") or error_dict.get("message")
            hint = (state.get("diagnosis") or {}).get("hint")
            last_error = raw_error
            if raw_error and hint:
                last_error = f"{raw_error}\nПодсказка регенерации: {hint}"
        result = service.generate(
            skeleton=skeleton,
            question=question,
            ctx=ctx,
            assumptions=state.get("assumptions") or [],
            permissions=state.get("permissions") or {},
            last_sql=state.get("sql") if on_fsm_retry else None,
            last_error=last_error,
            start_attempt=start_attempt,
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
            # Симметричная очистка (ревью S14 Minor-2): RETRYABLE не остаётся
            # stale после успеха ретрай-визита — корректность не держится
            # на неочевидном fuse-инварианте
            return {
                "sql": result.sql,
                "route_flags": {
                    **flags,
                    GENERATE_SQL_FAILED_FLAG: False,
                    GENERATE_SQL_RETRYABLE_FLAG: False,
                },
                "error": None,
                "budget_used": budget,
            }
        # FAIL: исчерпание M1 / шлюз / retrieval — отказ генератора; носитель —
        # degrade (ruling R2), КРОМЕ generation-fail на FSM-ретрай-визите:
        # битый выход модели — ретрай оправдан, узел ведёт ребро эскалации
        # (ruling R3/BL-016: визит 3 — strong при достатке бюджета)
        if tracer is not None and result.error:
            tracer.record(
                Trace(
                    event=EVENT_GENERATE_SQL_UNAVAILABLE,
                    payload={"user_id": user_id, "detail": result.error},
                )
            )
        message = result.clarifying_question or _GENERATE_UNAVAILABLE_MESSAGE
        retryable = (
            on_fsm_retry
            and result.outcome == GenerateSqlOutcome.FAIL
            and result.fail_kind == GenerateFailKind.GENERATION
        )
        return _fail(flags, "generate_sql_unavailable", message, budget, retryable=retryable)

    return node


def _fail(
    flags: dict[str, Any],
    error_class: str,
    message: str,
    budget: dict[str, Any] | None = None,
    *,
    retryable: bool = False,
) -> dict[str, Any]:
    """Отказ генератора: FLAG_GENERATE_SQL_FAILED → degrade (ruling R2 ARCH-GATE).

    ``retryable`` (ruling R3/BL-016) — generation-fail на FSM-ретрай-визите:
    дополнительно ставит FLAG_GENERATE_SQL_RETRYABLE — ребро
    route_generate_sql ведёт в гейт ретрая (fuse проверяет routing); при
    исчерпании fuse флаг бесполезен — degrade как раньше.
    """
    out_flags = {**flags, GENERATE_SQL_FAILED_FLAG: True}
    if retryable:
        out_flags[GENERATE_SQL_RETRYABLE_FLAG] = True
    update: dict[str, Any] = {
        "sql": None,
        "route_flags": out_flags,
        "error": {"class": error_class, "message": message},
    }
    if budget is not None:
        update["budget_used"] = budget
    return update
