"""Сборка StateGraph FSM — только рёбра и обёртки (S00 раскладка; ADR-002 §5).

Логика узлов — application/services (ruling R1 ARCH-GATE S04), решения рёбер —
routing.py, здесь — биндеры (трейс-спан узла + visited_nodes), гейты ретраев
(инкремент retry_count кодом ребра — Task 3 S04) и терминальные маркеры
(различимый payload исхода — state-agent v1.5). Узлы не знают о LangGraph:
замена движка возможна перестройкой только этого модуля (ADR-002 §5).

Шаговый цикл декомпозиции (hw-01 §3.5.4): execute_sql → merge_results →
generate_step_sql → validate_sql → …; рёбра merge_results → generate_step_sql
и probe-ветки заложены в каркасе (Ловушка-1 S04), активируются флагами
steps_pending/probe_lost_join (S14/S17).
"""

from collections.abc import Callable, Mapping
from typing import Any

from langgraph.graph import END, START, StateGraph

from text_to_sql_agent.application.ports.tracer import EVENT_GRAPH_NODE, Tracer
from text_to_sql_agent.application.services.stub_nodes import STUB_NODES
from text_to_sql_agent.domain.entities import (
    TERMINAL_OUTCOMES,
    AgentState,
    TerminalOutcome,
    Trace,
)
from text_to_sql_agent.orchestration.routing import (
    CONDITIONAL_ROUTES,
    FLAG_RETRY_TARGET,
    LLM_NODES,
    TRANSITION_FLAGS,
    FSMGuardCaps,
    make_guarded_route,
)

State = Mapping[str, Any]

# Гейты ретраев: имя гейта → ретрауемый узел. Гейт — код ребра ретрая: инкремент
# retry_count и сброс при смене ретрауемого узла (Task 3 S04, политика M1/D-016).
RETRY_GATES: dict[str, str] = {
    "retry_plan_joins": "plan_joins",  # plan_joins --само-ребро M1--> plan_joins
    "retry_generate_sql": "generate_sql",  # diagnose_error → generate_sql (попытка 2/3)
    "retry_generate_step_sql": "generate_step_sql",  # diagnose_error → шаг (D-019)
}


def make_retry_gate(target: str) -> Callable[[State], dict[str, Any]]:
    """Гейт ребра ретрая: инкремент retry_count кодом ребра, сброс при смене узла.

    Ретраится УЗЕЛ-цель (на diagnose_error → generate_sql счётчик инкрементирует
    generate_sql — ретраится генератор, не диагностика; ARCH-GATE S04). Целевой
    узел читает счётчик из state и по M1 выбирает роль попытки (S00 стык 2).
    """

    def gate(state: State) -> dict[str, Any]:
        flags = dict(state.get("route_flags") or {})
        count = state.get("retry_count") or 0
        if flags.get(FLAG_RETRY_TARGET) != target:
            count = 0  # сброс при смене ретрауемого узла
        flags[FLAG_RETRY_TARGET] = target
        return {"retry_count": count + 1, "route_flags": flags}

    return gate


def make_terminal(outcome: TerminalOutcome) -> Callable[[State], dict[str, Any]]:
    """Терминальный маркер: различимый payload исхода, затем END (state-agent v1.5).

    Все исходы — валидные done-состояния протокола, не исключения (ADR-002 §4).
    """

    def terminal(state: State) -> dict[str, Any]:
        return {"outcome": outcome}

    return terminal


def _content_binder(
    name: str, node_fn: Callable[[State], dict[str, Any]], tracer: Tracer | None
) -> Callable[[State], dict[str, Any]]:
    """Обёртка содержательного узла: трейс-спан + запись порядка обхода.

    visited_nodes пишут только содержательные узлы (гейты/терминалы — glue, их
    ход виден через outcome/трейс) — печать порядка в CLI берётся отсюда (R3).
    На ре-входе (цикл ретрая/шаговый цикл) одноразовые флаги перехода
    (TRANSITION_FLAGS — «шаг готов», «есть ещё шаги», исходы key-set) прошлой
    итерации гасятся: одноразовый переход не может сработать дважды, иначе
    каркас зацикливается на стейл-флагах; учёт шагов ведёт код шагов (S14/S17).
    Решенческие флаги (вердикт узла) не гасятся — узел перерешает их сам.
    Гашение ДОЛЖНО дойти до state: узел, вернувший частичное обновление без
    route_flags (S05+ реализации с частичными апдейтами), всё равно получает
    очищенную карту (ревью S04 I3 — инвариант не на побочном свойстве заглушек).
    """

    def binder(state: State) -> dict[str, Any]:
        if tracer is not None:
            tracer.record(Trace(event=EVENT_GRAPH_NODE, payload={"node": name}))
        prepared = state
        if name in (state.get("visited_nodes") or []):
            flags = state.get("route_flags") or {}
            prepared = {
                **state,
                "route_flags": {k: v for k, v in flags.items() if k not in TRANSITION_FLAGS},
            }
        updates = dict(node_fn(prepared))
        # Узел не трогал route_flags — очищенная карта уходит в state как есть
        # (для частичных апдейт S05+); вернул свои — его решение главнее
        updates.setdefault("route_flags", dict(prepared.get("route_flags") or {}))
        updates["visited_nodes"] = [*(state.get("visited_nodes") or []), name]
        return updates

    return binder


def build_fsm_app(
    *,
    tracer: Tracer | None = None,
    caps: FSMGuardCaps | None = None,
    checkpointer: Any | None = None,
    node_overrides: Mapping[str, Callable[[State], dict[str, Any]]] | None = None,
) -> Any:
    """Собрать компилированный граф FSM (15 узлов канона + glue рёбер).

    ``caps`` — глобальные капы прогона; None — гвард капов не ставится
    (тесты маршрутов без Settings). ``checkpointer`` — сессия interrupt-механики
    (BaseCheckpointSaver: Valkey в composition, MemorySaver в юнит-тестах;
    ruling R2 ARCH-GATE S08); None — граф без сессий (узлы без interrupt —
    каркасные тесты; clarify-узел с interrupt БЕЗ checkpointer — misuse,
    раннер переводит в fail, Ловушка 1 S08). ``node_overrides`` — подмена
    заглушек содержательными узлами (S05+: resolve_access и далее; сборка
    подмен — только composition, граф не правится — биндер принимает любую
    функцию). Сборка — только через composition (S00).
    """
    graph: StateGraph = StateGraph(AgentState)

    # 15 содержательных узлов канона (реестр — STUB_NODES; подмены —
    # node_overrides поверх реестра). Гвард глобальных капов — на ВСЕХ условных
    # рёбрах (и на рёбрах гейтов ретрая ниже): сработать он должен на ребре,
    # ВЕДУЩЕМ в узел с LLM (решение ребра = имя LLM-узла → llm_guard), не на
    # ребре «из» него (первый LLM-узел — clarify, входящее ребро — resolve_access,
    # который сам LLM не зовёт)
    nodes: dict[str, Callable[[State], dict[str, Any]]] = {
        **STUB_NODES,
        **(node_overrides or {}),
    }
    for name, node_fn in nodes.items():
        route = CONDITIONAL_ROUTES[name]
        if caps is not None:
            route = make_guarded_route(route, caps, LLM_NODES)
        graph.add_node(name, _content_binder(name, node_fn, tracer))
        graph.add_conditional_edges(name, route)

    # Гейты ретраев (код ребра: инкремент retry_count) → целевой узел. Ребро
    # гейта — тоже вход в LLM-узел: гвард капов обязателен и здесь (ревью S04 I1,
    # state-agent «до КАЖДОГО LLM-вызова»), иначе цикл ретрая входит в генератор
    # мимо исчерпанных капов
    for gate_name, target in RETRY_GATES.items():
        graph.add_node(gate_name, make_retry_gate(target))
        if caps is not None:
            graph.add_conditional_edges(
                gate_name, make_guarded_route(lambda _s, t=target: t, caps, LLM_NODES)
            )
        else:
            graph.add_edge(gate_name, target)

    # Терминальные маркеры → END (различимый payload исхода — outcome)
    for outcome in TERMINAL_OUTCOMES:
        terminal_name = f"terminal_{outcome}"
        graph.add_node(terminal_name, make_terminal(outcome))
        graph.add_edge(terminal_name, END)

    graph.add_edge(START, "resolve_access")
    return graph.compile(checkpointer=checkpointer)


# Fuse пер-узла: MAX_NODE_RETRIES (routing.py) проверяют условные рёбра
# plan_joins/diagnose_error; recursion_limit задаёт раннер (orchestration/runner.py).
