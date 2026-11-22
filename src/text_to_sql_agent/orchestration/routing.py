"""Условные рёбра FSM — маршрут назначает только код (hw-01 §2.1, ADR-002).

Маршрут читает исключительно ``route_flags``/``retry_count``/``budget_used``/
``deadline_at``/``error`` — кодовые поля state (карта владения — ``AgentState``;
``error`` пишет только код, успешный execute_sql обязан его очищать): LLM-текст
попадает только в содержательные поля и физически не может сменить ребро
(инвариант, проверяемый тестом). Ребра повторяют state-agent v1.5; источники
``overflow``/``short_circuit`` и ребро probe — по hw-01 §3.5.4 (шаговый цикл
execute_step; в диаграмме v1.4 входящие ребра не нарисованы — FIX-008).

Политика ретраев M1 (D-016, ADR-007 §4.1): инкремент ``retry_count`` — кодом
ребра ретрая (гейт-узлы graph.py), fuse ≤ 3 — здесь; эскалацию tier'ом назначает
код из того же счётчика (роль попытки узел берёт из state — S00 стык 2).
Рёбра clarify (само-ребро interrupt) и plan_joins → clarify — state-agent v1.6
(FIX-009, ruling R1 ARCH-GATE S08).
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from text_to_sql_agent.domain.entities import TERMINAL_OUTCOMES, TerminalOutcome

State = Mapping[str, Any]

# --- Имена флагов route_flags (единственный источник; см. владельцев в узлах) ---
FLAG_CLARIFICATION_NEEDED = "clarification_needed"  # clarify (S08)
FLAG_IDENTITY_FAILED = "identity_failed"  # resolve_access (S05) → rejected
FLAG_PLAN_JOINS_INVALID = "plan_joins_invalid"  # plan_joins: ретрай по M1
FLAG_PLAN_JOINS_EXHAUSTED = "plan_joins_exhausted"  # plan_joins → fail
FLAG_PLAN_JOINS_NO_PATH = "plan_joins_no_path"  # plan_joins: нет активного пути → degrade (S10, R2)
FLAG_PLAN_JOINS_CLARIFY = "plan_joins_clarify"  # планер: равные кандидаты → clarify (S10, FIX-009)
FLAG_T1_DECOMPOSE = "t1_decompose"  # порог T1 (feat-017) — код, не модель
FLAG_QUERY_PLAN_VALID = "query_plan_valid"  # plan_query: False → откат к одиночному SQL
FLAG_VALIDATE_HALLUCINATION = "validate_hallucination"  # validate_sql: галлюцинация → retry
FLAG_ACL_DENIED = "acl_denied"  # validate_sql: вне прав → degrade без retry (ADR-005)
FLAG_T2_HEAVY_ASYNC = "t2_heavy_async"  # check_cost: T2 → degraded_out (S12)
FLAG_T2_APPROXIMATE = "t2_approximate"  # check_cost: T2 → approximate (S12, D-019)
FLAG_BUDGET_EXCEEDED = "budget_exceeded"  # check_cost: стоимость > капа (плановая точка)
FLAG_STEP_DONE = "step_done"  # execute_sql: шаг декомпозиции готов (S14/S17)
FLAG_STEPS_PENDING = "steps_pending"  # merge_results: есть ещё шаги → generate_step_sql
FLAG_STEP_EMPTY_KEYSET = "step_empty_keyset"  # execute_sql: 0 ключей → short_circuit
FLAG_STEP_KEYSET_OVERFLOW = "step_keyset_overflow"  # execute_sql: key-set > капа → overflow
FLAG_PROBE_LOST_JOIN = "probe_lost_join"  # probe (S14): 0 строк — потерянный джойн → diagnose
FLAG_RETRY_GENERATE_SQL = "retry_generate_sql"  # diagnose: попытка 2/3 генератора (M1)
FLAG_RETRY_GENERATE_STEP_SQL = "retry_generate_step_sql"  # diagnose: ретрай шага (D-019)
FLAG_RETRY_TARGET = "retry_target"  # гейт: чей счётчик ретраев активен
FLAG_GENERATE_SQL_FAILED = "generate_sql_failed"  # генерация невозможна (исчерпание
# генератора / дефект скелета) → degrade (S10, ruling R2 ARCH-GATE)

# Одноразовые флаги перехода (шаговый цикл): описывают завершённый переход,
# а не вердикт узла — на ре-входе в узел гасятся биндером (graph.py), иначе
# каркас зациклился бы на стейл-флаге. Решенческие флаги (вердикт узла)
# не гасятся — их узел перерешает сам при ре-входе.
TRANSITION_FLAGS: frozenset[str] = frozenset(
    {
        FLAG_STEP_DONE,
        FLAG_STEPS_PENDING,
        FLAG_STEP_EMPTY_KEYSET,
        FLAG_STEP_KEYSET_OVERFLOW,
        FLAG_PROBE_LOST_JOIN,  # вердикт пробы (S14) — одноразовый: стейл-True
        # после ретрая уводил бы следующий прогон в diagnose_error мимо пробы
        FLAG_PLAN_JOINS_CLARIFY,  # переход планер → clarify (S08/S10): одноразовый —
        # иначе после resume clarify флаг уводил бы прогон в clarify повторно
    }
)

# --- Предохранители (оба уровня — код; ADR-002 §4) ---
# Per-node ≤ 3: попытка 1 + та же модель + эскалация уровнем вверх — политика M1
# (D-016, ADR-007 §4.1); почему 3 — канонная конвенция «≤ 3 попыток на узел»
# (S00 стык 1, hw-01 §6), не конфигурируется моделью.
MAX_NODE_RETRIES = 3

# Бюджетный учёт прогона в state.budget_used
BUDGET_LLM_CALLS_KEY = "llm_calls"
BUDGET_RUB_KEY = "rub"


@dataclass(frozen=True, slots=True)
class FSMGuardCaps:
    """Глобальные капы прогона (задаются composition из Settings, не моделью).

    ≤ 8 LLM-вызовов — канонный кап прогon (state-agent:100, hw-01 §6): теснее
    recursion_limit и срабатывает раньше по построению (каждый ретрай генерации
    = 1 вызов); ≤ 2 ₽ — бюджет запроса (Settings.budget_per_request_rub).
    """

    max_llm_calls: int
    budget_rub: float


def is_terminal(name: str) -> bool:
    return name in TERMINAL_OUTCOMES


def attempts_used(state: State, target: str) -> int:
    """Сколько попыток уже сделал узел ``target`` (политика M1).

    Счётчик ``retry_count`` валиден только для активного ретрая: гейт пишет
    ``route_flags[FLAG_RETRY_TARGET]`` — чужой счётчик (до этого ретраился другой
    узел) не ограничивает этот узел. Начальная попытка в счётчик не входит —
    это попытка 1 по M1 (попытка 2 — та же модель + ошибка, попытка 3 — уровень
    вверх; ADR-007 §4.1).
    """
    count = state.get("retry_count") or 0
    if (state.get("route_flags") or {}).get(FLAG_RETRY_TARGET) != target:
        return 1
    return count + 1


def llm_guard(state: State, caps: FSMGuardCaps) -> TerminalOutcome | None:
    """Глобальные капы — код-гвард до вызова узла с LLM (state-agent:100–103).

    Проверяет счётчик вызовов, бюджет ₽ и дедлайн прогона; сработавший кап —
    терминал budget_stop (частичный ответ — только с incomplete-флагом, S15).
    ``deadline_at`` — monotonic-метка оркестратора-раннера; None — дедлайн не
    задан (юнит-тесты routing без раннера).
    """
    budget = state.get("budget_used") or {}
    if budget.get(BUDGET_LLM_CALLS_KEY, 0) >= caps.max_llm_calls:
        return "budget_stop"
    if budget.get(BUDGET_RUB_KEY, 0.0) >= caps.budget_rub:
        return "budget_stop"
    deadline_at = state.get("deadline_at")
    if deadline_at is not None and time.monotonic() > deadline_at:
        return "budget_stop"
    return None


def route_resolve_access(state: State) -> str:
    """resolve_access → clarify | rejected (identity не проверена — fail-closed)."""
    if (state.get("route_flags") or {}).get(FLAG_IDENTITY_FAILED):
        return "terminal_rejected"
    return "clarify"


def route_clarify(state: State) -> str:
    """clarify → clarify (само-ребро interrupt) | clarification | plan_joins.

    Приоритет причин (rulings R1–R3 ARCH-GATE S08): вопрос сформулирован
    (вердикт классификатора ИЛИ равные кандидаты планера — S10) → само-ребро
    clarify → clarify: ре-вход узла вызывает interrupt() — пауза до ответа
    пользователя (checkpointer, TTL 10 мин); после resume узел пишет допущения
    и чистит вопрос → план_joins. Флаг БЕЗ вопроса — честный отказ (узел не
    смог сформулировать вопрос — LLM-сбой, S00-8): терминал clarification.
    Заглушка S04 (флаг False) — план_joins.
    """
    if state.get("clarify_question"):
        return "clarify"  # само-ребро: interrupt-механика (visit 2) — R1/R2
    if (state.get("route_flags") or {}).get(FLAG_CLARIFICATION_NEEDED):
        return "terminal_clarification"
    return "plan_joins"


def route_plan_joins(state: State) -> str:
    """plan_joins → build_skeleton | plan_query (T1) | clarify | degrade (нет пути) | ретрай | fail.

    Fuse «per-node ≤ 3 попыток» (S00 стык 1) срабатывает раньше флага ретрая;
    флаг exhausted — решение узла (M1: «попытки исчерпаны»). Ребро
    plan_joins → clarify — равные кандидаты пути (hw-01 §4.2; FIX-009: ребро
    заложено S08 по прецеденту «ветки в каркасе», активирует S10). Ребро
    plan_joins → degrade — «нет активного пути» (behavior feat-009 + заявка
    владельцу; hw-01 §3.1:113; отказ формулирует S15 — ruling R2 ARCH-GATE S10).
    """
    flags = state.get("route_flags") or {}
    exhausted = attempts_used(state, "plan_joins") >= MAX_NODE_RETRIES
    if flags.get(FLAG_PLAN_JOINS_EXHAUSTED) or exhausted:
        return "terminal_fail"
    if flags.get(FLAG_PLAN_JOINS_NO_PATH):
        return "degrade"  # нет активного пути — отказ + заявка (S15 формулирует)
    if flags.get(FLAG_PLAN_JOINS_CLARIFY):
        return "clarify"  # равные кандидаты → уточнение (interrupt, S08/S10)
    if flags.get(FLAG_PLAN_JOINS_INVALID):
        return "retry_plan_joins"  # гейт: инкремент кодом ребра → plan_joins
    if flags.get(FLAG_T1_DECOMPOSE):
        return "plan_query"  # T1 (≥ 4 таблиц / ≥ 3 хопов) — код, feat-017
    return "build_skeleton"


def route_build_skeleton(state: State) -> str:
    """build_skeleton → generate_sql | plan_query (T1) | degrade (скелет не собран).

    Скелет не собрался — дефект каркаса (узел build_skeleton fail-closed):
    генерация невозможна, честная деградация (ruling R2 ARCH-GATE S10 — единый
    флаг FLAG_GENERATE_SQL_FAILED с отказом генератора).
    """
    flags = state.get("route_flags") or {}
    if flags.get(FLAG_GENERATE_SQL_FAILED):
        return "degrade"
    if flags.get(FLAG_T1_DECOMPOSE):
        return "plan_query"
    return "generate_sql"


def route_plan_query(state: State) -> str:
    """plan_query → generate_step_sql (план валиден) | generate_sql (откат).

    Заглушка «не декомпозировать» держит query_plan_valid=False — откат к
    одиночному SQL (канонное ребро, ADR-004 v1.3); валидацию инвариантов плана
    делает код (feat-017).
    """
    if (state.get("route_flags") or {}).get(FLAG_QUERY_PLAN_VALID):
        return "generate_step_sql"
    return "generate_sql"


def route_generate_sql(state: State) -> str:
    """generate_sql → validate_sql | degrade (исчерпание M1 — ruling R2 ARCH-GATE S10).

    Валидация не перепрыгивается: единственное ребро к исполнению проходит через
    validate_sql — свойство структуры (ADR-002 §5: «ребро execute_sql существует
    только из validate_sql»). Отказ генератора (ретраи исчерпаны/бюджет/дефект
    скелета) → degrade: носитель отказа генератора при исчерпании — канон
    degrade (state-agent; отказ формулирует S15 бизнес-понятиями).
    """
    if (state.get("route_flags") or {}).get(FLAG_GENERATE_SQL_FAILED):
        return "degrade"
    return "validate_sql"


def route_generate_step_sql(state: State) -> str:
    """generate_step_sql → validate_sql (шаг валидируется как полноценный SQL)."""
    return "validate_sql"


def route_validate_sql(state: State) -> str:
    """validate_sql → check_cost (ok) | diagnose_error (галлюцинация) | degrade.

    Различение причин (ADR-005): «не существует» — галлюцинация, retry оправдан;
    «вне прав» — сигнал безопасности, retry не оправдан (модель не уговорит
    валидатор) — деградация.
    """
    flags = state.get("route_flags") or {}
    if flags.get(FLAG_ACL_DENIED):
        return "degrade"  # без retry (модель не должна «уговорить» валидатор)
    if flags.get(FLAG_VALIDATE_HALLUCINATION):
        return "diagnose_error"
    return "check_cost"


def route_check_cost(state: State) -> str:
    """check_cost → execute_sql | budget_stop (плановая точка) | T2-исходы.

    T2 approximate/matview — через compact_result → interpret_result с флагом
    approximate (пользователь получает объяснение — D-019); async/отказ —
    терминал degraded_out (неполное не выдаётся за полное).
    """
    flags = state.get("route_flags") or {}
    if flags.get(FLAG_BUDGET_EXCEEDED):
        return "terminal_budget_stop"
    if flags.get(FLAG_T2_HEAVY_ASYNC):
        return "terminal_degraded_out"
    if flags.get(FLAG_T2_APPROXIMATE):
        return "compact_result"
    return "execute_sql"


def route_execute_sql(state: State) -> str:
    """execute_sql → шаговый цикл | probe-решение | diagnose_error.

    Приоритет причин (state-agent v1.5): SQL-ошибка → diagnose_error; шаговый
    цикл: пустой key-set → short_circuit («данных нет», hw-01 §3.5.4 правило 2),
    key-set > капа 50 000 → overflow (молча обрезать нельзя); шаг готов →
    merge_results; 0 строк одиночного запроса → probe-решение (COUNT-проба —
    S14; каркас: «реально пусто» — валидный пустой ответ → compact_result).
    """
    flags = state.get("route_flags") or {}
    if state.get("error"):
        return "diagnose_error"
    if flags.get(FLAG_STEP_EMPTY_KEYSET):
        return "terminal_short_circuit"
    if flags.get(FLAG_STEP_KEYSET_OVERFLOW):
        return "terminal_overflow"
    if flags.get(FLAG_STEP_DONE):
        return "merge_results"
    # 0 строк одиночного запроса: probe (COUNT) — ребро, не узел (state-agent).
    # Обе ветки probe заложены в каркасе (Ловушка-1): COUNT-проба выполняется S14
    # и ставит probe_lost_join при подозрении на потерянный джойн → diagnose_error;
    # каркас (проба не выполняется) — «реально пусто» — валидный пустой ответ (S00-8)
    if flags.get(FLAG_PROBE_LOST_JOIN):
        return "diagnose_error"
    return "compact_result"


def route_merge_results(state: State) -> str:
    """merge_results → generate_step_sql (есть ещё шаги) | compact_result (все готовы)."""
    if (state.get("route_flags") or {}).get(FLAG_STEPS_PENDING):
        return "generate_step_sql"
    return "compact_result"


def route_diagnose_error(state: State) -> str:
    """diagnose_error → ретрай генератора (гейт) | degrade (лимит/бюджет/дедлайн).

    Ретраится упавший шаг, не весь план (D-019): флаг шагового ретрая старше.
    Fuse «per-node ≤ 3 попыток» читает счётчик ретрауемого узла (инкремент —
    гейт на ребре ретрая); эскалация tier'ом (попытка 3 при достатке бюджета
    с резервом 0,6 ₽ — S14).
    """
    flags = state.get("route_flags") or {}
    if flags.get(FLAG_RETRY_GENERATE_STEP_SQL):
        if attempts_used(state, "generate_step_sql") < MAX_NODE_RETRIES:
            return "retry_generate_step_sql"
        return "degrade"
    if flags.get(FLAG_RETRY_GENERATE_SQL):
        if attempts_used(state, "generate_sql") < MAX_NODE_RETRIES:
            return "retry_generate_sql"
        return "degrade"
    return "degrade"


def route_compact_result(state: State) -> str:
    """compact_result → interpret_result (безусловно: объяснение строится на компакте)."""
    return "interpret_result"


def route_interpret_result(state: State) -> str:
    """interpret_result → terminal_done (ответ: текст + SQL + допущения + флаги)."""
    return "terminal_done"


def route_degrade(state: State) -> str:
    """degrade → terminal_degrade (деградация: отказ/доступный вариант, схема скрыта)."""
    return "terminal_degrade"


def make_guarded_route(
    route: Callable[[State], str], caps: FSMGuardCaps, llm_targets: frozenset[str]
) -> Callable[[State], str]:
    """Обвязка условного ребра к узлу с LLM: глобальные капы — до вызова узла.

    «Бюджет-гвард проверяется до каждого LLM-вызова; в диаграмме плановая точка
    check_cost» (state-agent). Сработавший кап направляет в терминал budget_stop,
    иначе — решение исходного ребра.
    """

    def guarded(state: State) -> str:
        decision = route(state)
        if decision in llm_targets:
            stop = llm_guard(state, caps)
            if stop is not None:
                return f"terminal_{stop}"
        return decision

    return guarded


# Реестр условных рёбер по именам узлов графа (единственный источник маршрутов;
# graph.py подключает по этому реестру — состав рёбер проверяется тестом)
CONDITIONAL_ROUTES: dict[str, Callable[[State], str]] = {
    "resolve_access": route_resolve_access,
    "clarify": route_clarify,
    "plan_joins": route_plan_joins,
    "build_skeleton": route_build_skeleton,
    "plan_query": route_plan_query,
    "generate_sql": route_generate_sql,
    "generate_step_sql": route_generate_step_sql,
    "validate_sql": route_validate_sql,
    "check_cost": route_check_cost,
    "execute_sql": route_execute_sql,
    "merge_results": route_merge_results,
    "diagnose_error": route_diagnose_error,
    "compact_result": route_compact_result,
    "interpret_result": route_interpret_result,
    "degrade": route_degrade,
}

# Узлы, вызывающие LLM (гвард глобальных капов — на входящих рёбрах этих узлов,
# включая ребро через гейт ретрая): clarify/plan_joins/plan_query — «средняя/
# reasoning» (hw-01 §3.5.4), generate_sql/generate_step_sql — генераторы,
# diagnose_error — разбор (S14), interpret_result/degrade — объяснение/отказ (S15)
LLM_NODES: frozenset[str] = frozenset(
    {
        "clarify",
        "plan_joins",
        "plan_query",
        "generate_sql",
        "generate_step_sql",
        "diagnose_error",
        "interpret_result",
        "degrade",
    }
)
