"""Заглушки-транзиты 15 узлов FSM (S04, веха V4) — логика узлов в application
(ruling R1 ARCH-GATE S04: orchestration — только рёбра и биндеры, прецедент формы
— execute_sql_node.py).

Каждая заглушка — транзит: читает/пишет ТОЛЬКО свои поля state (карта владения —
docstring ``AgentState``), маршрут не решает (его назначает граф — ADR-002),
LLM/БД не трогает (содержательное наполнение — S05/S08/S10/S11/S14/S15/S12/S17).

Семантика флагов — setdefault: узел фиксирует в ``route_flags`` решение
СВОЕЙ зоны (константы ``FLAG_*`` orchestration/routing.py), НО не переопределяет
уже выставленное значение. Основание: маршрут пишет только код (S00 стык 1);
уже выставленный флаг — кодовое решение предыдущего звена (инъекция теста
симулирует то, что будущие сессии напишут кодом узла). Так одна и та же
заглушка работает и в happy-path, и в тестах веток без второй реализации.

Правда заглушек (не выдавать за содержательное поведение):
- resolve_access: identity приходит проверенной (S00: аутентификация вне скоупа),
  права — пустой scope, graph_version — 0 (снапшот не загружен, ADR-006 §4.3);
  наполнение — S05 (resolve_access_node + ResolveAccessService), пин снапшота — S07.
- clarify: вопрос однозначен, допущения фиксирует код ([] — решение вехи V3,
  hw-plan §4); interrupt()/checkpointer — S08.
- plan_joins: JoinPlan не строится (пути графа — S09, подключение — S10);
  T1 не активен (порог — код, feat-017).
- build_skeleton: скелет не строится (наполнение — S10).
- plan_query: «не декомпозировать» (feat-017 — выпускная; T2-порог — S12).
- generate_sql/generate_step_sql: SQL не генерируется (S10/S14).
- validate_sql: валидация не выполняется (S11 — sqlglot AST + ACL + граф);
  по умолчанию «ok» — валидный транзит, не «пропущенная проверка» (проверки
  ещё не существуют, ребро галлюцинации/deny уже заложено — Ловушка-1 S04).
- check_cost: транзит (порог T2 не активен до S12, hw-01 §3.4).
- execute_sql: SQL не исполняется (подключение узла feat-013 — S14); флаги
  шагового цикла (short_circuit/overflow/steps_pending) — зона S14/S17.
- merge_results/compact_result/interpret_result/diagnose_error/degrade:
  транзиты своего поля (S14/S15).
"""

from collections.abc import Mapping
from typing import Any

from text_to_sql_agent.domain.entities import AgentState

State = Mapping[str, Any]


def _flags(state: State) -> dict[str, Any]:
    return dict(state.get("route_flags") or {})


def _with_flags(state: State, own: dict[str, Any]) -> dict[str, Any]:
    """setdefault-семантика: своё решение не затирает уже выставленный флаг."""
    flags = _flags(state)
    for key, value in own.items():
        flags.setdefault(key, value)
    return flags


def _update(state: State, **fields: Any) -> dict[str, Any]:
    """Частичное обновление state (каналы LangGraph мержатся по ключу)."""
    return dict(fields)


def _bump_llm_calls(state: State) -> dict[str, Any]:
    """Имитация расхода LLM-вызова: инкремент budget_used.llm_calls (учёт прогона).

    Заглушки узлов из LLM_NODES «платят» по вызову за вход (карта владения
    AgentState: «учёт узлов с LLM; заглушки имитируют») — глобальный гвард капов
    (routing.llm_guard) работает end-to-end уже в каркасе: цикл ретраев
    упирается в ≤ 8 вызовов, а не только в пер-узловый fuse. Содержательные
    сервисы (S05+) заменят имитацию учётом фактического usage провайдера.
    """
    budget = dict(state.get("budget_used") or {})
    budget["llm_calls"] = (budget.get("llm_calls") or 0) + 1
    return {"budget_used": budget}


def resolve_access_stub(state: State) -> dict[str, Any]:
    """Узел resolve_access (S05): identity проверена, права + graph_version в state."""
    flags = _with_flags(
        state,
        {
            # fail-closed ветка rejected — решение S05; каркас: identity проверена
            "identity_failed": False,
        },
    )
    return _update(
        state,
        permissions={},  # stub: пустой scope — AuthProvider/настройки читает S05
        graph_version=0,  # stub: снапшот не загружен (ADR-006 §4.3); синк — S07
        route_flags=flags,
    )


def clarify_stub(state: State) -> dict[str, Any]:
    """Узел clarify (S08): допущения зафиксированы, уточнение не требуется.

    Канон-заглушка: «уточнение — валидный исход» (ADR-002), ветка clarification
    существует ребром уже в каркасе (Ловушка-1); interrupt()/checkpointer — S08.
    """
    return _update(
        state,
        rewritten_question=None,  # stub: переформулировка не требуется
        assumptions=[],  # допущения фиксирует код (решение вехи V3, hw-plan §4)
        route_flags=_with_flags(state, {"clarification_needed": False}),
        **_bump_llm_calls(state),
    )


def plan_joins_stub(state: State) -> dict[str, Any]:
    """Узел plan_joins (S09 — сервис есть, подключение к FSM — S10): JoinPlan нет.

    Флаги зоны узла: невалидный план (ретрай по M1), исчерпание попыток (fail),
    T1-декомпозиция (порог — код feat-017).
    """
    return _update(
        state,
        join_plan=None,  # stub: план не строится (S09-сервис — S10 подключит)
        route_flags=_with_flags(
            state,
            {
                "plan_joins_invalid": False,
                "plan_joins_exhausted": False,
                "t1_decompose": False,  # порог T1 не активен до feat-017
            },
        ),
        **_bump_llm_calls(state),
    )


def build_skeleton_stub(state: State) -> dict[str, Any]:
    """Узел build_skeleton (S10): JOIN-скелет пишет код, здесь — транзит."""
    return _update(
        state,
        skeleton=None,  # stub: скелет не строится (S10)
        skeleton_hash=None,  # хэш скелета для валидатора (S10/S11)
    )


def plan_query_stub(state: State) -> dict[str, Any]:
    """Узел plan_query (feat-017): заглушка «не декомпозировать» (ADR-004 v1.3).

    План невалиден → откат к одиночному SQL — канонное ребро plan_query →
    generate_sql; T1-декомпозиция на стенде — заглушка (hw-plan §4).
    """
    return _update(
        state, route_flags=_with_flags(state, {"query_plan_valid": False}), **_bump_llm_calls(state)
    )


def generate_sql_stub(state: State) -> dict[str, Any]:
    """Узел generate_sql (S10): наполнение скелета LLM, здесь — транзит."""
    return _update(state, sql=None, **_bump_llm_calls(state))  # stub: SQL не генерируется (S10)


def generate_step_sql_stub(state: State) -> dict[str, Any]:
    """Узел generate_step_sql (S14): SQL шага декомпозиции, здесь — транзит (D-019)."""
    return _update(
        state, sql=None, **_bump_llm_calls(state)
    )  # stub: SQL шага не генерируется (S14)


def validate_sql_stub(state: State) -> dict[str, Any]:
    """Узел validate_sql (S11): sqlglot AST + ACL + джойны по графу — не активен.

    Ребро «ok» — дефолт транзита; ветки галлюцинации (retry через diagnose_error)
    и deny (деградация без retry — ADR-005) существуют ребрами уже в каркасе.
    """
    return _update(
        state,
        validation={"ok": True},  # stub: проверки S11 ещё не подключены
        route_flags=_with_flags(
            state,
            {
                "validate_hallucination": False,  # таблицы/связи нет — retry
                "acl_denied": False,  # вне прав — degrade без retry (ADR-005)
            },
        ),
    )


def check_cost_stub(state: State) -> dict[str, Any]:
    """Узел check_cost (S12): транзит — порог T2 не активен до feat-012.

    Глобальные капы прогона (≤ 8 вызовов / ≤ 90 s / ≤ 2 ₽) проверяет код-гвард
    на рёбрах перед узлами с LLM (routing.py) — плановая точка budget_stop
    здесь, в check_cost (state-agent: контракт budget_stop).
    """
    return _update(
        state,
        route_flags=_with_flags(
            state,
            {
                "t2_heavy_async": False,  # T2 → async/отказ — терминал degraded_out
                "t2_approximate": False,  # T2 → approximate/matview (S12, D-019)
                "budget_exceeded": False,  # стоимость превысила кап 2 ₽
            },
        ),
    )


def execute_sql_stub(state: State) -> dict[str, Any]:
    """Узел execute_sql (feat-013 есть, подключение — S14): SQL не исполняется.

    Пустой результат — валидный исход (S00-8): stub отдаёт честное «0 строк»,
    probe-решение (COUNT — S14) на ребре трактуется «реально пусто» (ветка
    probe_lost_join → diagnose_error заложена ребром, активирует S14).
    Флаги шагового цикла — зона S14/S17, заглушка их не пишет (инъекция теста
    симулирует код шагового контура: short_circuit/overflow/steps_pending).
    """
    return _update(
        state,
        rows=[],  # stub: исполнения нет — валидный пустой результат
        row_count=0,
        truncated=False,
        error=None,
    )


def merge_results_stub(state: State) -> dict[str, Any]:
    """Узел merge_results (S14): слияние шагов кодом, здесь — транзит.

    Ребро steps_pending → generate_step_sql (шаговый цикл, hw-01 §3.5.4)
    заложено в графе; заглушка его не активирует.
    """
    return _update(state)


def compact_result_stub(state: State) -> dict[str, Any]:
    """Узел compact_result (S15): row_count + ≤ 20 строк + агрегаты, здесь — транзит."""
    return _update(state, compact=None)  # stub: компакт не строится (S15)


def diagnose_error_stub(state: State) -> dict[str, Any]:
    """Узел diagnose_error (S14): разбор ошибки + политика M1, здесь — транзит.

    Ретраится генератор (generate_sql / generate_step_sql — D-019), не диагностика:
    флаги ретрая читает ребро, счётчик инкрементирует гейт целевого узла.
    Без флагов — лимит попыток/бюджет/дедлайн → degrade (канонное ребро).
    """
    return _update(
        state,
        route_flags=_with_flags(
            state,
            {
                "retry_generate_sql": False,  # попытка 2/3 по M1 (ADR-007 §4.1)
                "retry_generate_step_sql": False,  # ретрай шага, не всего плана
            },
        ),
        **_bump_llm_calls(state),
    )


def interpret_result_stub(state: State) -> dict[str, Any]:
    """Узел interpret_result (S15): объяснение на языке бизнеса, здесь — транзит.

    В каркасе терминал done получает state как есть — содержательный ответ
    (текст + SQL + meaning рёбер + допущения + флаги + стоимость) собирает S15.
    """
    return _update(state, **_bump_llm_calls(state))


def degrade_stub(state: State) -> dict[str, Any]:
    """Узел degrade (S15): отказ/доступный вариант бизнес-понятиями, схема скрыта.

    ADR-005: «вне прав» — сигнал безопасности, retry не оправдан; код фиксирует,
    чего не хватает, LLM формулирует отказ (S15). В каркасе — транзит в терминал.
    """
    return _update(state, **_bump_llm_calls(state))


STUB_NODES: dict[str, Any] = {
    # Состав 15 узлов hw-01 §2.1 / state-agent v1.5 — единственный реестр каркаса;
    # имена ключей графа = имена узлов канона (тест состава смотрит сюда)
    "resolve_access": resolve_access_stub,
    "clarify": clarify_stub,
    "plan_joins": plan_joins_stub,
    "build_skeleton": build_skeleton_stub,
    "plan_query": plan_query_stub,
    "generate_sql": generate_sql_stub,
    "generate_step_sql": generate_step_sql_stub,
    "validate_sql": validate_sql_stub,
    "check_cost": check_cost_stub,
    "execute_sql": execute_sql_stub,
    "merge_results": merge_results_stub,
    "compact_result": compact_result_stub,
    "diagnose_error": diagnose_error_stub,
    "interpret_result": interpret_result_stub,
    "degrade": degrade_stub,
}


def initial_state(question: str, user_id: str = "") -> "AgentState":
    """Стартовый state прогона: контракт стыка 1, дефолты нейтральны.

    ``user_id`` — verified identity (ruling R4 ARCH-GATE S05: AuthProvider.verify
    в точке входа ДО прогона; пусто — узел resolve_access fail-closed → rejected).
    Флаги усечения — False (неполное не выдаётся за полное — S15 выставит);
    route_flags/retry_count/visited_nodes — нейтральные кодовые дефолты;
    clarify_question/interrupted/trace_id — interrupt-механика и корреляция
    прогона (rulings R1–R3 ARCH-GATE S08: trace_id/interrupted ставит раннер,
    clarify_question — узел clarify при неоднозначном вердикте).
    """
    return {
        "question": question,
        "user_id": user_id,
        "rewritten_question": None,
        "assumptions": [],
        "clarify_question": None,
        "permissions": {},
        "graph_version": None,
        "join_plan": None,
        "skeleton": None,
        "skeleton_hash": None,
        "sql": None,
        "validation": None,
        "rows": None,
        "row_count": None,
        "compact": None,
        "error": None,
        "retry_count": 0,
        "budget_used": {"llm_calls": 0, "rub": 0.0},
        "route_flags": {},
        "truncated": False,
        "approximate": False,
        "incomplete": False,
        "visited_nodes": [],
        "outcome": None,
        "deadline_at": None,
        "trace_id": None,
        "interrupted": False,
    }
