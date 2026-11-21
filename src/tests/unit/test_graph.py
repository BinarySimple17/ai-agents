"""Юнит-тесты каркаса FSM (S04, feat-004): состав 15 узлов, достижимость
терминальных исходов, порядок happy-path, предохранители, инвариант маршрута.

Маршрут пишет только код (S00 стык 1): тесты инъекцией route_flags в стартовый
state СИМУЛИРУЮТ кодовое решение будущих сессий (S05+/S14/S17 напишут те же
флаги из содержательных сервисов) — инвариант «модель не меняет маршрут»
не ослабляется: routing читает только кодовые поля state.
"""

from typing import Any

import pytest
from langgraph.errors import GraphRecursionError

from text_to_sql_agent.application.ports.llm_provider import BudgetExceededError, LLMRequestError
from text_to_sql_agent.application.services.stub_nodes import initial_state
from text_to_sql_agent.application.use_cases.answer_question import AnswerQuestion, FSMResult
from text_to_sql_agent.domain.entities import TERMINAL_OUTCOMES
from text_to_sql_agent.orchestration.graph import RETRY_GATES, build_fsm_app
from text_to_sql_agent.orchestration.routing import (
    LLM_NODES,
    MAX_NODE_RETRIES,
    FSMGuardCaps,
    attempts_used,
)
from text_to_sql_agent.orchestration.runner import RECURSION_LIMIT_ERROR_CLASS, LangGraphRunner

QUESTION = "Сколько заказов у клиентов из РФ?"
CAPS = FSMGuardCaps(max_llm_calls=8, budget_rub=2.0)

# Канонный happy path (state-agent v1.5, T1 выключен — порог feat-017):
# plan_query/generate_step_sql/diagnose_error/merge_results/degrade — ветки
# (достижимость доказывают отдельные тесты), не узлы основного пути
HAPPY_PATH = [
    "resolve_access",
    "clarify",
    "plan_joins",
    "build_skeleton",
    "generate_sql",
    "validate_sql",
    "check_cost",
    "execute_sql",
    "compact_result",
    "interpret_result",
]


def _invoke(flags: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    app = build_fsm_app(caps=CAPS)
    state = initial_state(QUESTION)
    if flags:
        state["route_flags"] = dict(flags)
    state.update(overrides)
    return app.invoke(state, config={"recursion_limit": 50})


class TestComposition:
    def test_fifteen_canon_nodes_registered(self) -> None:
        app = build_fsm_app()
        node_names = set(app.nodes) - {"__start__"}
        canon = {
            "resolve_access",
            "clarify",
            "plan_joins",
            "build_skeleton",
            "plan_query",
            "generate_sql",
            "generate_step_sql",
            "validate_sql",
            "check_cost",
            "execute_sql",
            "merge_results",
            "compact_result",
            "diagnose_error",
            "interpret_result",
            "degrade",
        }
        assert canon <= node_names, "узел канона не зарегистрирован в графе"

    def test_glue_nodes_present(self) -> None:
        # Гейты ретраев (инкремент кодом ребра) и терминальные маркеры — glue
        app = build_fsm_app()
        node_names = set(app.nodes)
        assert set(RETRY_GATES) <= node_names
        assert {f"terminal_{outcome}" for outcome in TERMINAL_OUTCOMES} <= node_names

    def test_all_nine_terminal_outcomes_reachable(self) -> None:
        # Каждая ветка — своим кодовым флагом (state-agent v1.5; источники
        # overflow/short_circuit — шаговый цикл execute_sql, hw-01 §3.5.4/FIX-008)
        branch_flags: dict[str, dict[str, Any]] = {
            "done": {},
            "clarification": {"clarification_needed": True},
            "rejected": {"identity_failed": True},
            "fail": {"plan_joins_exhausted": True},
            "degrade": {"acl_denied": True},
            "budget_stop": {"budget_exceeded": True},
            "degraded_out": {"t2_heavy_async": True},
            "short_circuit": {"step_empty_keyset": True},
            "overflow": {"step_keyset_overflow": True},
        }
        assert set(branch_flags) == set(TERMINAL_OUTCOMES)
        for outcome, flags in branch_flags.items():
            assert _invoke(flags)["outcome"] == outcome, f"терминал {outcome} недостижим"


class TestHappyPath:
    def test_order_of_nodes(self) -> None:
        final = _invoke()
        assert final["visited_nodes"] == HAPPY_PATH
        assert final["outcome"] == "done"

    def test_t1_branch_goes_through_plan_query(self) -> None:
        # T1 (код, feat-017): plan_joins → plan_query → заглушка «не
        # декомпозировать» → откат к одиночному SQL (канонное ребро)
        final = _invoke({"t1_decompose": True})
        assert final["visited_nodes"] == [
            "resolve_access",
            "clarify",
            "plan_joins",
            "plan_query",
            "generate_sql",
            "validate_sql",
            "check_cost",
            "execute_sql",
            "compact_result",
            "interpret_result",
        ]

    def test_step_cycle_edge_exists(self) -> None:
        # Шаговый цикл (Ловушка-1: ребро заложено в каркасе, активирует S14/S17):
        # execute_sql(шаг готов) → merge_results → generate_step_sql → validate_sql
        final = _invoke({"step_done": True, "steps_pending": True, "retry_generate_step_sql": True})
        nodes = final["visited_nodes"]
        assert (
            nodes.index("execute_sql")
            < nodes.index("merge_results")
            < nodes.index("generate_step_sql")
        )
        assert final["outcome"] == "done"


class TestRouteFlagInvariant:
    def test_unknown_flags_do_not_change_route(self) -> None:
        # LLM-текст попадает только в содержательные поля: мусор в route_flags
        # (например, выдуманное «next_node») маршрут не меняет
        final = _invoke({"next_node": "interpret_result", "skip_validation": True})
        assert final["visited_nodes"] == HAPPY_PATH

    def test_llm_like_content_in_state_does_not_change_route(self) -> None:
        # Содержательное поле с «именем узла» — данные, не маршрут (ADR-002)
        final = _invoke(sql="interpret_result", join_plan={"next_node": "fail"})
        assert final["outcome"] == "done"


class TestPerNodeFuse:
    def test_plan_joins_loop_fails_after_three_attempts(self) -> None:
        # Само-ребро M1 (план невалиден): гейт инкрементирует счётчик, fuse ≤ 3
        # попыток → fail-терминал (отказ + уточняющий вопрос), не исключение
        final = _invoke({"plan_joins_invalid": True})
        attempts = final["visited_nodes"].count("plan_joins")
        assert attempts == MAX_NODE_RETRIES
        assert final["outcome"] == "fail"

    def test_retry_counter_resets_on_target_switch(self) -> None:
        # diagnose_error → generate_step_sql после ретраев generate_sql:
        # счётчик шага — свой (сброс при смене ретрауемого узла)
        from text_to_sql_agent.orchestration.graph import make_retry_gate

        gate_sql = make_retry_gate("generate_sql")
        state: dict[str, Any] = {"route_flags": {}, "retry_count": 0}
        state.update(gate_sql(state))
        assert state["retry_count"] == 1
        state.update(gate_sql(state))
        assert state["retry_count"] == 2
        # Смена ретрауемого узла — счётчик с нуля (изоляция fuse узлов)
        gate_step = make_retry_gate("generate_step_sql")
        state.update(gate_step(state))
        assert state["retry_count"] == 1
        assert state["route_flags"]["retry_target"] == "generate_step_sql"

    def test_attempts_used_ignores_foreign_counter(self) -> None:
        state: dict[str, Any] = {"retry_count": 3, "route_flags": {"retry_target": "generate_sql"}}
        assert attempts_used(state, "generate_step_sql") == 1  # чужой счётчик не кусается
        assert attempts_used(state, "generate_sql") == 4

    def test_diagnose_fuse_degrades_instead_of_looping(self) -> None:
        # diagnose_error с флагом ретрая на исчерпанном счётчике → degrade
        # (лимит попыток), не четвёртая генерация
        from text_to_sql_agent.orchestration.routing import route_diagnose_error

        state: dict[str, Any] = {
            "retry_count": MAX_NODE_RETRIES - 1,
            "route_flags": {"retry_target": "generate_sql", "retry_generate_sql": True},
        }
        assert route_diagnose_error(state) == "degrade"


class TestGlobalCaps:
    def test_llm_calls_cap_stops_before_first_llm_node(self) -> None:
        # Гвард — на ребре В llm-узел (clarify — первый): resolve_access прошёл,
        # дальше — budget_stop (кап ≤ 8 вызовов, state-agent:100)
        final = _invoke(budget_used={"llm_calls": 8, "rub": 0.0})
        assert final["outcome"] == "budget_stop"
        assert final["visited_nodes"] == ["resolve_access"]

    def test_rub_cap_stops_run(self) -> None:
        final = _invoke(budget_used={"llm_calls": 0, "rub": 2.0})
        assert final["outcome"] == "budget_stop"

    def test_deadline_cap_stops_run(self) -> None:
        # Дедлайн 90 s в прошлом (монотонная метка раннера) — budget_stop
        final = _invoke(deadline_at=0.0)
        assert final["outcome"] == "budget_stop"

    def test_under_caps_run_completes(self) -> None:
        final = _invoke(budget_used={"llm_calls": 2, "rub": 0.4})
        assert final["outcome"] == "done"

    def test_stubs_pay_llm_call_on_entry(self) -> None:
        # Ревью I1(a): учёт живой — вход в LLM-узел инкрементирует счётчик
        # (заглушки имитируют, карта владения budget_used); happy path из
        # 4 LLM-узлов = 4 вызова, под капом
        final = _invoke()
        assert final["budget_used"]["llm_calls"] == 4

    @pytest.mark.parametrize("node", sorted(LLM_NODES))
    def test_every_llm_node_pays_one_call(self, node: str) -> None:
        # Ревью-2 минор-3: инвариант «узел ∈ LLM_NODES ⟺ заглушка платит» —
        # дрейф (добавили узел в LLM_NODES, забыли bump) остаётся зелёным
        # только без этого теста; гвард капов тогда молча протекает
        from text_to_sql_agent.application.services.stub_nodes import STUB_NODES

        state = initial_state(QUESTION)
        updates = STUB_NODES[node](state)
        budget = updates.get("budget_used") or state.get("budget_used") or {}
        assert budget.get("llm_calls") == 1, f"заглушка {node} не платит вызовом"

    def test_retry_edge_is_guarded_by_caps(self) -> None:
        # Ревью I1(b), регрессия: ребро ГЕЙТА ретрая — тоже вход в LLM-узел.
        # Счётчик подобран так, чтобы кап исчерпался на первом входе plan_joins:
        # clarify платит 7-й, plan_joins (невалидный план) — 8-й; гейт обязан
        # завернуть прогон в budget_stop, а не впустить 2-ю попытку.
        # До фикса: гейт вёл в plan_joins безусловно — fuse отрабатывал «fail».
        final = _invoke(
            {"plan_joins_invalid": True},
            budget_used={"llm_calls": 6, "rub": 0.0},
        )
        assert final["outcome"] == "budget_stop"
        assert final["visited_nodes"].count("plan_joins") == 1


class TestProbeBranch:
    def test_zero_rows_with_lost_join_flag_goes_to_diagnose(self) -> None:
        # Ревью I2, регрессия: ветка probe «подозрение на потерянный джойн»
        # заложена ребром в каркасе (Ловушка-1); активирует COUNT-проба S14.
        # 0 строк + probe_lost_join → diagnose_error → (заглушка без ретрая) degrade
        final = _invoke({"probe_lost_join": True})
        assert final["outcome"] == "degrade"
        nodes = final["visited_nodes"]
        assert nodes.index("execute_sql") < nodes.index("diagnose_error") < nodes.index("degrade")

    def test_zero_rows_without_flag_is_valid_empty_answer(self) -> None:
        # Каркас: проба не выполняется — «реально пусто» (S00-8), не ошибка
        final = _invoke()
        assert final["outcome"] == "done"
        assert "diagnose_error" not in final["visited_nodes"]


class TestBinderTransitionFlags:
    def test_reentry_clears_transition_flags_for_partial_nodes(self) -> None:
        # Ревью I3, регрессия: гашение одноразовых флагов обязано доходить до
        # state даже когда узел вернул частичное обновление БЕЗ route_flags
        # (S05+ реализации с частичными апдейтами) — иначе стейл-флаг вернёт
        # шаговый цикл в маршрут и прогон упрётся в recursion_limit
        from text_to_sql_agent.orchestration.graph import _content_binder

        binder = _content_binder("execute_sql", lambda state: {}, tracer=None)
        state: dict[str, Any] = {
            "visited_nodes": ["execute_sql"],  # ре-вход
            "route_flags": {"step_done": True, "steps_pending": True, "acl_denied": False},
        }
        state["route_flags"]["probe_lost_join"] = True  # вердикт пробы (S14)
        updates = binder(state)
        assert "step_done" not in updates["route_flags"]
        assert "steps_pending" not in updates["route_flags"]
        assert "probe_lost_join" not in updates["route_flags"]  # одноразовый (ревью-2)
        assert updates["route_flags"]["acl_denied"] is False  # решенческие не тронуты

    def test_first_entry_keeps_injected_flags(self) -> None:
        # Первый вход — инъекция теста (симуляция кодового решения) выживает
        from text_to_sql_agent.orchestration.graph import _content_binder

        binder = _content_binder("execute_sql", lambda state: {}, tracer=None)
        state: dict[str, Any] = {
            "visited_nodes": [],  # первый вход
            "route_flags": {"step_empty_keyset": True},
        }
        updates = binder(state)
        assert updates["route_flags"]["step_empty_keyset"] is True


class TestRunner:
    def test_recursion_limit_becomes_fail_outcome(self) -> None:
        # Второй предохранитель (ADR-002 §4): GraphRecursionError на границе
        # движка → валидный отказ с различимым payload, не исключение
        class ExplodingApp:
            def invoke(self, state: dict, config: dict) -> dict:
                raise GraphRecursionError("limit exceeded")

        runner = LangGraphRunner(ExplodingApp(), deadline_s=90.0, recursion_limit=50)
        result = runner.run(initial_state(QUESTION))
        assert result["outcome"] == "fail"
        assert result["error"]["class"] == RECURSION_LIMIT_ERROR_CLASS

    def test_runner_sets_deadline_and_completes(self) -> None:
        app = build_fsm_app(caps=CAPS)
        runner = LangGraphRunner(app, deadline_s=90.0, recursion_limit=50)
        result = runner.run(initial_state(QUESTION))
        assert result["outcome"] == "done"
        assert result["deadline_at"] is not None  # monotonic-метка вшита в state

    def test_injected_deadline_is_honored(self) -> None:
        # Инъекция теста (прошедший дедлайн) не перезаписывается — fuse честный
        app = build_fsm_app(caps=CAPS)
        runner = LangGraphRunner(app, deadline_s=90.0, recursion_limit=50)
        state = initial_state(QUESTION)
        state["deadline_at"] = 0.0
        assert runner.run(state)["outcome"] == "budget_stop"


class _StubLLM:
    """Двойник LLMProvider для use case: каркас LLM не зовёт — только reset."""

    def __init__(self) -> None:
        self.resets = 0

    def reset_request_budget(self) -> None:
        self.resets += 1

    def complete(self, role: str, messages: Any, *, tools: Any = None) -> None:
        raise AssertionError("узлы-заглушки не зовут LLM в каркасе S04")


class _StubRunner:
    """Двойник GraphRunner: возвращает заданный state или бросает."""

    def __init__(self, outcome_state: dict[str, Any] | None = None, error: Exception | None = None):
        self.seen: list[dict[str, Any]] = []
        self.outcome_state = outcome_state
        self.error = error

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        self.seen.append(dict(state))
        if self.error is not None:
            raise self.error
        return dict(self.outcome_state or {})


class TestAnswerQuestion:
    def test_resets_request_budget_on_start(self) -> None:
        # Шов S03 (ruling Q7 S09): прогон начинает запрос с нуля
        llm = _StubLLM()
        runner = _StubRunner({"outcome": "done", "visited_nodes": list(HAPPY_PATH)})
        result = AnswerQuestion(runner=runner, llm=llm).run(QUESTION)
        assert llm.resets == 1
        assert result.outcome == "done"
        assert result.visited_nodes == HAPPY_PATH
        # Стартовый state ушёл в раннер с вопросом и нейтральными дефолтами
        assert runner.seen[0]["question"] == QUESTION
        assert runner.seen[0]["retry_count"] == 0

    def test_provider_budget_error_maps_to_budget_stop(self) -> None:
        # Кап бюджета адаптера (S03) поднимается в УЗЛЕ (внутри графа) и
        # доходит до use case через invoke — budget_stop, не traceback
        llm = _StubLLM()
        result = AnswerQuestion(
            runner=_StubRunner(error=BudgetExceededError("кап 2 ₽")), llm=llm
        ).run(QUESTION)
        assert result.outcome == "budget_stop"
        assert "кап 2 ₽" in result.state["error"]["message"]

    def test_provider_unavailable_maps_to_degrade(self) -> None:
        # S00-8: недоступность шлюза — валидный исход, не traceback
        result = AnswerQuestion(
            runner=_StubRunner(error=LLMRequestError("ретраи исчерпаны")), llm=_StubLLM()
        ).run(QUESTION)
        assert result.outcome == "degrade"
        assert result.state["error"]["class"] == "LLMRequestError"

    def test_missing_outcome_is_fail_not_silent(self) -> None:
        # Дефект сборки графа (раннер вернул state без outcome) — честный fail
        result = AnswerQuestion(runner=_StubRunner({"visited_nodes": []}), llm=_StubLLM()).run(
            QUESTION
        )
        assert result.outcome == "fail"
        assert "без терминального исхода" in result.state["error"]["message"]

    def test_fsm_result_answer_property(self) -> None:
        result = FSMResult(outcome="done", state={"compact": {"row_count": 5}})
        assert result.answer == {"row_count": 5}
