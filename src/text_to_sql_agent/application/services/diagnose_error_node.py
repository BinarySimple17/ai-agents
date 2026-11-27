"""Узел diagnose_error для FSM (feat-014, S14) — glue сервис ↔ state (прецедент
формы: validate_sql_node S11, generate_sql_node S10).

Вход ретрай-цикла M1: читает ``state.error`` (SQL-ошибка S13 / вердикт
валидатора S11 / probe-подозрение S14), ``state.validation.issues`` (внутренний
контекст галлюцинации) и ``state.sql`` (последняя попытка). Разбор — сервис
(futile-классы без LLM); решение «ретраить ли генератор» — КОД: попытка
определяется счётчиком гейта (route_flags[retry_target] + retry_count, S00
стык 2), fuse ≤ 3 — узел не назначает ретрай при исчерпании (вторую проверку
делает ребро routing.route_diagnose_error). Итог — ``state.diagnosis``
(класс + подсказка + futile; читает generate_sql для retry-контекста D-016)
и флаг ``retry_generate_sql`` (вершит ребро diagnose_error → retry_generate_sql
→ generate_sql). Ретрай шага (generate_step_sql — D-019) в базовом цикле не
назначается: ребро-заглушка до feat-017.

Валидный выход узла НЕ трогает ``state.error.message`` (пользовательский
фиксированный текст S9 не смешивается с внутренними подсказками) и НЕ чистит
его: ошибку очищает успешный generate_sql (карта владения AgentState).
"""

from collections.abc import Mapping
from typing import Any

from text_to_sql_agent.application.ports.tracer import (
    EVENT_DIAGNOSE_UNAVAILABLE,
    EVENT_DIAGNOSE_VERDICT,
    Tracer,
)
from text_to_sql_agent.application.services.diagnose_error_service import (
    DiagnoseResult,
    DiagnoseService,
)
from text_to_sql_agent.application.services.m1_policy import MAX_ATTEMPTS
from text_to_sql_agent.domain.entities import Trace

# application не импортирует orchestration — имена флагов дублируются из
# routing.FLAG_*; совпадение строк гвардит parity-тест (прецедент S05/S08/S10/S11)
RETRY_GENERATE_SQL_FLAG = "retry_generate_sql"
# Гейт ретрая пишет имя ретрауемого узла в route_flags (S00 стык 2)
RETRY_TARGET_FLAG = "retry_target"
RETRY_TARGET_GENERATE_SQL = "generate_sql"

# Валидатор S11 пишет исход в state.validation["outcome"] (ADR-005 §4)
_VALIDATION_HALLUCINATION = "hallucination"

State = dict[str, Any]


def _attempts_used(state: State) -> int:
    """Попытки генератора по счётчику гейта (зеркало routing.attempts_used).

    Счётчик валиден только для активного ретрая генератора; начальная попытка
    в счётчик не входит (попытка 1 по M1). Зеркало — потому что application не
    импортирует orchestration; согласованность гвардит parity-тест routing.
    """
    count = state.get("retry_count") or 0
    if (state.get("route_flags") or {}).get(RETRY_TARGET_FLAG) != RETRY_TARGET_GENERATE_SQL:
        return 1
    return count + 1


def _plan_tables(state: State) -> frozenset[str] | None:
    """Таблицы плана из skeleton (S33 A4: мир подсказки semantics).

    diagnose_error — не владелец skeleton: отсутствие/мусор не дефект (план
    проверяет validate_sql на своём ребре) — подсказка просто без раздела
    «доступные колонки» (fallback сервиса).
    """
    raw = state.get("skeleton")
    if not isinstance(raw, dict) or not isinstance(raw.get("tables"), (list, tuple)):
        return None
    names = frozenset(str(t).strip().lower() for t in raw["tables"] if str(t).strip())
    return names or None


def make_diagnose_error_node(
    service: DiagnoseService,
    *,
    tracer: Tracer | None = None,
) -> Any:
    """Фабрика узла diagnose_error: разбор + назначение ретрая генератору."""

    def node(state: State) -> dict[str, Any]:
        passthrough_flags = dict(state.get("route_flags") or {})
        user_id = (state.get("user_id") or "").strip()
        if not user_id:
            # Defense-in-depth: identity проверена resolve_access — пустой user_id
            # означает подмену каркаса; fail-closed (прецедент generate_sql_node)
            return _unavailable(
                passthrough_flags,
                tracer,
                "identity_missing",
                "identity отсутствует в state — подмена каркаса",
            )
        raw_error = state.get("error")
        if not isinstance(raw_error, dict) or not raw_error.get("class"):
            # diagnose_error достигается только по рёбрам с выставленной ошибкой
            # (validate_hallucination / execute_sql / probe) — отсутствие означает
            # подмену каркаса; fail-closed: без ретрая (degrade-ветка routing)
            return _unavailable(
                passthrough_flags,
                tracer,
                "error_missing",
                "state.error отсутствует — diagnose_error достигнут вне цикла сбоя",
            )

        validation = state.get("validation")
        validation_issues: tuple[str, ...] = ()
        if isinstance(validation, dict) and validation.get("outcome") == _VALIDATION_HALLUCINATION:
            issues = validation.get("issues")
            if isinstance(issues, (list, tuple)):
                validation_issues = tuple(str(item) for item in issues)
        # Латентный инвариант (re-review S33, контрольный вопрос 2): issues
        # непусты ⟺ вердикт валидатора hallucination; execute_sql — единственный
        # преемник check_cost ← validate_sql, и OK-вердикт пишет outcome=ok с
        # пустыми issues — на semantics-сбое исполнителя issues гарантированно
        # пусты, гейт специфичности подсказки (issues подавляют detail/каталог)
        # не глотает сырой текст БД. Изменение этой связки — пересмотр гейта.

        result: DiagnoseResult = service.diagnose(
            error_class=str(raw_error.get("class")),
            error_message=str(raw_error.get("message") or ""),
            sql=state.get("sql") if isinstance(state.get("sql"), str) else None,
            validation_issues=validation_issues,
            # Внутренние факты COUNT-пробы (execute_sql пишет в error["probe"],
            # S9: CLI печатает только message) — контекст для подсказки регенерации
            probe_context=(
                str(raw_error["probe"]) if isinstance(raw_error.get("probe"), str) else None
            ),
            # S33: сырой текст БД (error["detail"] — внутренний канал execute_sql)
            # и таблицы плана — факты для код-подсказки semantics (без LLM);
            # права пользователя — грант-фильтр раздела «доступные колонки»
            # (ревью S33 I-1: имена вне гранта во внешний LLM не отдаются)
            error_detail=(
                str(raw_error["detail"]) if isinstance(raw_error.get("detail"), str) else None
            ),
            plan_tables=_plan_tables(state),
            permissions=(
                state["permissions"] if isinstance(state.get("permissions"), Mapping) else None
            ),
        )
        budget = dict(state.get("budget_used") or {})
        budget["llm_calls"] = (budget.get("llm_calls") or 0) + result.llm_calls

        if result.verdict is None:
            # Разбор не удался (шлюз/M1) — ретрай не назначаем: деградация
            # валидный исход (S00-8); деталь сбоя — оператору (S9)
            if tracer is not None and result.error:
                tracer.record(
                    Trace(
                        event=EVENT_DIAGNOSE_UNAVAILABLE,
                        payload={"user_id": user_id, "detail": result.error},
                    )
                )
            return {
                "budget_used": budget,
                "route_flags": passthrough_flags,
                "error": raw_error,
            }

        if tracer is not None:
            tracer.record(
                Trace(
                    event=EVENT_DIAGNOSE_VERDICT,
                    payload={
                        "user_id": user_id,
                        "error_class": result.verdict.error_class.value,
                        "retry_futile": result.verdict.retry_futile,
                        "llm_calls": result.llm_calls,
                    },
                )
            )
        diagnosis = {
            "error_class": result.verdict.error_class.value,
            "hint": result.verdict.regeneration_hint,
            "retry_futile": result.verdict.retry_futile,
        }
        update: dict[str, Any] = {
            "diagnosis": diagnosis,
            "budget_used": budget,
            "error": raw_error,  # не трогаем: очищает успешный generate_sql
            "route_flags": passthrough_flags,
        }
        if not result.verdict.retry_futile and _attempts_used(state) < MAX_ATTEMPTS:
            update["route_flags"] = {
                **passthrough_flags,
                RETRY_GENERATE_SQL_FLAG: True,
            }
        return update

    return node


def _unavailable(
    passthrough_flags: dict[str, Any],
    tracer: Tracer | None,
    defect_class: str,
    detail: str,
) -> dict[str, Any]:
    """Дефект каркаса/стыка: fail-closed без назначения ретрая (degrade-ветка routing).

    Деталь дефекта — оператору в трейс (S9-прецедент skeleton.unavailable);
    state.diagnosis не пишется — разбора не было, подсказке неоткуда взяться.
    """
    if tracer is not None:
        tracer.record(
            Trace(
                event=EVENT_DIAGNOSE_UNAVAILABLE,
                payload={"class": defect_class, "detail": detail},
            )
        )
    return {"route_flags": passthrough_flags}
