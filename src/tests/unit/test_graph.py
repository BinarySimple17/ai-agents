"""Юнит-тесты каркаса FSM (S04, feat-004): состав 15 узлов, достижимость
терминальных исходов, порядок happy-path, предохранители, инвариант маршрута.

Маршрут пишет только код (S00 стык 1): тесты инъекцией route_flags в стартовый
state СИМУЛИРУЮТ кодовое решение будущих сессий (S05+/S14/S17 напишут те же
флаги из содержательных сервисов) — инвариант «модель не меняет маршрут»
не ослабляется: routing читает только кодовые поля state.
"""

from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError

from text_to_sql_agent.application.ports.checkpointer import CheckpointStoreError
from text_to_sql_agent.application.ports.llm_provider import BudgetExceededError, LLMRequestError
from text_to_sql_agent.application.services.clarify_node import make_clarify_node
from text_to_sql_agent.application.services.clarify_service import (
    ClarifyOutcome,
    ClarifyResult,
)
from text_to_sql_agent.application.services.stub_nodes import initial_state
from text_to_sql_agent.application.use_cases.answer_question import AnswerQuestion, FSMResult
from text_to_sql_agent.domain.entities import TERMINAL_OUTCOMES, Assumption
from text_to_sql_agent.orchestration.graph import RETRY_GATES, build_fsm_app
from text_to_sql_agent.orchestration.interrupt_gate import LangGraphInterruptGate
from text_to_sql_agent.orchestration.routing import (
    FLAG_CLARIFICATION_NEEDED,
    FLAG_PLAN_JOINS_CLARIFY,
    FLAG_PLAN_JOINS_INVALID,
    LLM_NODES,
    MAX_NODE_RETRIES,
    TRANSITION_FLAGS,
    FSMGuardCaps,
    attempts_used,
    route_clarify,
    route_plan_joins,
)
from text_to_sql_agent.orchestration.runner import (
    RECURSION_LIMIT_ERROR_CLASS,
    LangGraphRunner,
    build_fsm_runner,
)

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


class TestClarifyRouting:
    """Ветвление после clarify (S08/feat-008, rulings R1-R3): вопрос в state —
    само-ребро interrupt-механики; флаг без вопроса — честный отказ; иначе —
    план_joins. Ребро plan_joins -> clarify — равные кандидаты (FIX-009/R1)."""

    def test_pending_question_loops_back_into_clarify(self) -> None:
        assert route_clarify({"clarify_question": "Уточните период"}) == "clarify"

    def test_flag_without_question_is_honest_clarification_terminal(self) -> None:
        # Узел не смог сформулировать вопрос (LLM-сбой) — валидный исход (S00-8)
        state = {"route_flags": {FLAG_CLARIFICATION_NEEDED: True}, "clarify_question": None}
        assert route_clarify(state) == "terminal_clarification"

    def test_neither_question_nor_flag_goes_to_plan_joins(self) -> None:
        assert route_clarify({"route_flags": {}}) == "plan_joins"
        assert route_clarify({"route_flags": {FLAG_CLARIFICATION_NEEDED: False}}) == "plan_joins"

    def test_plan_joins_clarify_flag_routes_back_to_clarify(self) -> None:
        # Равные кандидаты пути (S10 активирует) — повторный вход clarify
        state = {"route_flags": {FLAG_PLAN_JOINS_CLARIFY: True, FLAG_PLAN_JOINS_INVALID: True}}
        assert route_plan_joins(state) == "clarify"

    def test_plan_joins_exhausted_still_wins_over_clarify(self) -> None:
        state = {
            "route_flags": {FLAG_PLAN_JOINS_CLARIFY: True, "plan_joins_exhausted": True},
            "retry_count": 0,
        }
        assert route_plan_joins(state) == "terminal_fail"

    def test_plan_joins_clarify_flag_is_one_shot_transition(self) -> None:
        # Одноразовый переход (биндером гасится на ре-входе) — иначе после resume
        # clarify стейл-True уводил бы прогон в clarify повторно
        assert FLAG_PLAN_JOINS_CLARIFY in TRANSITION_FLAGS


class _ScriptedClarifyService:
    """Скриптовый clarify-сервис: один вердикт на прогон (LLM — мок, S00-7)."""

    def __init__(self, result: ClarifyResult) -> None:
        self._result = result
        self.called = 0

    def clarify(self, question: str, *, ctx: Any, permissions: Any) -> ClarifyResult:
        self.called += 1
        return self._result


def _rights_passing_node(state: dict[str, Any]) -> dict[str, Any]:
    # Минимальная подмена resolve_access: права в state, identity-флаг не ставится
    return {"permissions": {"orders": ["*"]}, "graph_version": 0}


class _RecordingTracer:
    """Фейковый Tracer: копит записи для проверок (прецедент test_clarify)."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    def record(self, trace: Any) -> None:
        self.records.append(trace)


class TestInterruptFlow:
    """interrupt()/checkpointer (Task 2 S08, rulings R2/R3/R6): пауза -> resume
    ТОГО ЖЕ прогона; TTL истёк -> clarification-терминал; misuse — fail, не
    traceback. Checkpointer в unit — MemorySaver; живой Valkey — integration."""

    def _runner(
        self, result: ClarifyResult, tracer: Any | None = None
    ) -> tuple[Any, _ScriptedClarifyService]:
        service = _ScriptedClarifyService(result)
        return (
            build_fsm_runner(
                tracer=tracer,
                caps=CAPS,
                checkpointer=MemorySaver(),
                deadline_s=90.0,
                recursion_limit=50,
                node_overrides={
                    "resolve_access": _rights_passing_node,
                    "clarify": make_clarify_node(service, LangGraphInterruptGate()),
                },
            ),
            service,
        )

    def test_ambiguous_question_pauses_graph_with_marker(self) -> None:
        runner, _ = self._runner(
            ClarifyResult(outcome=ClarifyOutcome.CLARIFY, question="За какой период?")
        )
        final = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-int")
        assert final["interrupted"] is True
        assert final.get("outcome") is None  # не терминал — пауза (ruling R3)
        assert final["clarify_question"] == "За какой период?"

    def test_resume_same_thread_continues_same_run(self) -> None:
        # Ловушка 2 S08: resume по тому же thread_id — ТОТ ЖЕ прогон (допущение
        # user_answer в state, прогон доходит до done, не новый invocation)
        tracer = _RecordingTracer()
        runner, service = self._runner(
            ClarifyResult(outcome=ClarifyOutcome.CLARIFY, question="За какой период?"),
            tracer=tracer,
        )
        paused = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-same")
        assert paused["interrupted"] is True
        final = runner.resume("за 2026 год", thread_id="t-same")
        assert final["outcome"] == "done"
        assert final["interrupted"] is False
        # Событие фактической паузы — ровно одно на прогон (зона раннера;
        # тот же tracer несёт и graph.node-спаны биндера)
        pauses = [e for e in tracer.records if e.event == "clarify.interrupt"]
        assert len(pauses) == 1
        assert pauses[0].payload["question"] == "За какой период?"
        assert pauses[0].payload["thread_id"] == "t-same"
        answers = [a for a in final["assumptions"] if a.source == "user_answer"]
        assert [a.text for a in answers] == ["за 2026 год"]
        # Классификация выполнялась РОВНО ОДИН раз (visit 1) — ruling R6:
        # повторный LLM-вызов на resume недопустим
        assert service.called == 1
        assert final["visited_nodes"].count("clarify") == 2  # классификация + interrupt

    def test_expired_session_synthesizes_clarification_terminal(self) -> None:
        # TTL истёк (ruling R3): валидный clarification-исход, не исключение;
        # state восстановить неоткуда — payload из question
        runner, _ = self._runner(ClarifyResult(outcome=ClarifyOutcome.CLARIFY, question="Уточните"))
        runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-exp")
        runner._app.checkpointer.delete_thread("t-exp")  # имитация истёкшего TTL
        final = runner.resume("ответ", thread_id="t-exp", question=QUESTION)
        assert final["outcome"] == "clarification"
        assert final["interrupted"] is False
        assert final["error"]["class"] == "clarify_session_expired"
        assert final["question"] == QUESTION

    def test_resume_without_interrupt_is_fail_not_crash(self) -> None:
        # Resume по завершённому прогону — дефект протокола вызова; граница
        # движка: fail-payload, не ValueError наружу
        runner, _ = self._runner(ClarifyResult(outcome=ClarifyOutcome.UNAMBIGUOUS))
        runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-done")
        final = runner.resume("ответ", thread_id="t-done")
        assert final["outcome"] == "fail"
        assert final["error"]["class"] == "resume_without_interrupt"

    def test_run_emits_agent_start_once_resume_does_not(self) -> None:
        # S27 Task 1: трейс FSM-прогона начинается с вопроса (agent.start,
        # mode=fsm) — «вопрос принят» с trace_id раннера; resume продолжает
        # ТОТ ЖЕ прогон — повторного «вопрос принят» нет
        tracer = _RecordingTracer()
        runner, _ = self._runner(
            ClarifyResult(outcome=ClarifyOutcome.CLARIFY, question="За какой период?"),
            tracer=tracer,
        )
        runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-start")
        starts = [e for e in tracer.records if e.event == "agent.start"]
        assert len(starts) == 1
        payload = starts[0].payload
        assert payload["question"] == QUESTION
        assert payload["user_id"] == "u_analyst"
        assert payload["mode"] == "fsm"
        assert payload["trace_id"]  # trace_id ставит раннер — корреляция трейса
        final = runner.resume("за 2026 год", thread_id="t-start")
        assert final["outcome"] == "done"
        assert len([e for e in tracer.records if e.event == "agent.start"]) == 1

    def test_interrupt_without_checkpointer_is_fail_not_traceback(self) -> None:
        # Ловушка 1 S08: interrupt без checkpointer = GraphInterrupt наружу —
        # раннер переводит в честный fail (misuse сборки)
        service = _ScriptedClarifyService(
            ClarifyResult(outcome=ClarifyOutcome.CLARIFY, question="Уточните")
        )
        runner = build_fsm_runner(
            caps=CAPS,
            checkpointer=None,  # сессии нет
            deadline_s=90.0,
            recursion_limit=50,
            node_overrides={
                "resolve_access": _rights_passing_node,
                "clarify": make_clarify_node(service, LangGraphInterruptGate()),
            },
        )
        final = runner.run(initial_state(QUESTION, user_id="u_analyst"))
        assert final["outcome"] == "fail"
        assert final["error"]["class"] == "interrupt_without_checkpointer"

    def test_unambiguous_question_never_pauses(self) -> None:
        runner, _ = self._runner(
            ClarifyResult(
                outcome=ClarifyOutcome.UNAMBIGUOUS,
                assumptions=[Assumption(source="classifier", text="период — 2026")],
            )
        )
        final = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-ok")
        assert final["outcome"] == "done"
        assert final["interrupted"] is False
        assert [a.text for a in final["assumptions"]] == ["период — 2026"]

    def test_resume_rewinds_deadline(self, monkeypatch) -> None:
        # Ruling R6 (ревью S08 Important-3a): дедлайн 90 s — кап ОБРАБОТКИ, не
        # ожидания пользователя; раннер перевзводит deadline_at при resume.
        # Управляемые часы: пауза при t=100 (дедлайн 190), «ожидание» до t=200 —
        # без перевзвода гвард капов остановил бы продолжение budget_stop-ом
        clock = _FakeClock(100.0)
        monkeypatch.setattr("text_to_sql_agent.orchestration.runner.time.monotonic", clock)
        runner, _ = self._runner(ClarifyResult(outcome=ClarifyOutcome.CLARIFY, question="Уточните"))
        paused = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-dl")
        assert paused["interrupted"] is True
        assert paused["deadline_at"] == 190.0
        clock.t = 200.0  # старый дедлайн (190) истёк за время «ожидания»
        final = runner.resume("ответ", thread_id="t-dl")
        assert final["outcome"] == "done"  # перевзвод: дедлайн 290, гвард пропускает
        assert final["deadline_at"] == 290.0

    def test_resume_keeps_budget_counters(self) -> None:
        # Ruling R6 (ревью S08 Important-3b): капы персистятся в checkpointer'е —
        # resume НЕ сбрасывает budget_used; исчерпанный кап останавливает
        # продолжение budget_stop-ом, а не начинает «свежий» прогон
        runner, _ = self._runner(
            ClarifyResult(outcome=ClarifyOutcome.CLARIFY, question="Уточните", llm_calls=0)
        )
        state = initial_state(QUESTION, user_id="u_analyst")
        state["budget_used"] = {"llm_calls": CAPS.max_llm_calls - 1, "rub": 0.0}
        paused = runner.run(state, thread_id="t-cap")
        assert paused["interrupted"] is True
        final = runner.resume("ответ", thread_id="t-cap")
        # plan_joins-заглушка платит вызов (7→8), гвард на ребре в generate_sql
        # останавливает прогон: счётчик пережил паузу
        assert final["outcome"] == "budget_stop"
        assert final["budget_used"]["llm_calls"] == CAPS.max_llm_calls


class _FakeClock:
    """Управляемые часы (детерминизм тестов R6): runner/routing читают monotonic."""

    def __init__(self, start: float) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t


class _BrokenPutSaver(MemorySaver):
    """Хранилище сессий недоступно на записи (Valkey упал) — I2."""

    def put(self, *args: Any, **kwargs: Any) -> Any:
        raise CheckpointStoreError(
            "хранилище сессий недоступно: 127.0.0.1:6379 соединение разорвано"
        )


class TestInterruptStoreFailure:
    """I2 (ревью S08): сбой Valkey в FSM-пути — валидный fail-исход, не traceback
    (S00-8); пользовательский текст фиксированный, деталь — операторскому трейсу."""

    def _runner(self, checkpointer: Any, tracer: Any | None = None) -> Any:
        service = _ScriptedClarifyService(
            ClarifyResult(outcome=ClarifyOutcome.CLARIFY, question="Уточните")
        )
        return build_fsm_runner(
            tracer=tracer,
            caps=CAPS,
            checkpointer=checkpointer,
            deadline_s=90.0,
            recursion_limit=50,
            node_overrides={
                "resolve_access": _rights_passing_node,
                "clarify": make_clarify_node(service, LangGraphInterruptGate()),
            },
        )

    def test_store_failure_on_run_is_valid_fail(self) -> None:
        tracer = _RecordingTracer()
        runner = self._runner(_BrokenPutSaver(), tracer=tracer)
        final = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-broken")
        assert final["outcome"] == "fail"
        assert final["error"]["class"] == "checkpoint_unavailable"
        assert final["error"]["message"] == "хранилище сессий недоступно — повторите запрос позже"
        assert "127.0.0.1" not in final["error"]["message"]  # S9: деталь не пользователю
        store_events = [e for e in tracer.records if e.event == "checkpoint.unavailable"]
        assert len(store_events) == 1
        assert "127.0.0.1" in store_events[0].payload["detail"]  # оператору — диагностика

    def test_store_failure_on_resume_is_valid_fail(self, monkeypatch) -> None:
        runner = self._runner(MemorySaver())
        paused = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-b2")
        assert paused["interrupted"] is True
        saver = runner._app.checkpointer

        def broken(*args: Any, **kwargs: Any) -> Any:
            raise CheckpointStoreError("соединение разорвано при чтении сессии")

        monkeypatch.setattr(saver, "get_tuple", broken)
        final = runner.resume("ответ", thread_id="t-b2", question=QUESTION)
        assert final["outcome"] == "fail"
        assert final["error"]["class"] == "checkpoint_unavailable"
        assert "127.0.0.1" not in final["error"]["message"]

    def test_value_error_on_resume_is_honest_engine_error(self, monkeypatch) -> None:
        # Re-review M-r2 (Minor-8): остаточный ValueError движка — engine_error,
        # НЕ маскируется под resume_without_interrupt (тот случай — гвард tasks)
        runner = self._runner(MemorySaver())
        paused = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-ve")
        assert paused["interrupted"] is True

        def broken_invoke(*args: Any, **kwargs: Any) -> Any:
            raise ValueError("канал не принял обновление дедлайна")

        monkeypatch.setattr(runner._app, "invoke", broken_invoke)
        final = runner.resume("ответ", thread_id="t-ve", question=QUESTION)
        assert final["outcome"] == "fail"
        assert final["error"]["class"] == "engine_error"
        assert "канал не принял" in final["error"]["message"]


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
    """Двойник GraphRunner: возвращает заданный state или бросает.

    Сигнатура с ``thread_id`` — расширение порта ruling R3 ARCH-GATE S08
    (interrupt-сессия; каркасные тесты его не передают).
    """

    def __init__(self, outcome_state: dict[str, Any] | None = None, error: Exception | None = None):
        self.seen: list[dict[str, Any]] = []
        self.resumed: list[tuple[str, str, str | None]] = []
        self.outcome_state = outcome_state
        self.error = error

    def run(self, state: dict[str, Any], *, thread_id: str | None = None) -> dict[str, Any]:
        self.seen.append(dict(state))
        if self.error is not None:
            raise self.error
        return dict(self.outcome_state or {})

    def resume(self, answer: str, *, thread_id: str, question: str | None = None) -> dict[str, Any]:
        self.resumed.append((answer, thread_id, question))
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
        # S15: наружный payload — final_answer (interpret/degrade); compact —
        # внутренний вид для промпта интерпретатора, наружу не идёт
        payload = {"text": "Всего 5 заказов.", "row_count": 5}
        result = FSMResult(outcome="done", state={"final_answer": payload, "compact": {"x": 1}})
        assert result.answer == payload
        assert FSMResult(outcome="done", state={"compact": {"row_count": 5}}).answer is None

    def test_paused_result_carries_thread_id_for_resume(self) -> None:
        # Ревью S08 Important-1: пауза с ненаружным thread_id — тупик (resume
        # нечем вызвать); use case генерирует id сам и ВОЗВРАЩАЕТ его
        llm = _StubLLM()
        paused_state = {"interrupted": True, "clarify_question": "Уточните"}
        use_case = AnswerQuestion(runner=_StubRunner(paused_state), llm=llm)
        result = use_case.run(QUESTION)  # thread_id не передан
        assert result.paused is True  # реальная пауза (маркер раннера в payload)
        assert result.thread_id, "FSMResult.thread_id пуст — resume нечем вызвать"
        runner = use_case._runner  # noqa: SLF001
        assert isinstance(runner, _StubRunner)
        assert runner.resumed == []  # сам resume не вызывался
        resumed = use_case.resume("ответ", thread_id=result.thread_id)
        assert runner.resumed[0][1] == result.thread_id  # тот же id — тот же прогон
        assert resumed.thread_id == result.thread_id  # инвариант поля и на resume

    def test_repaused_resume_carries_same_thread_id(self) -> None:
        # Ревью S08 re-review Minor-1: resume может снова приостановиться
        # (ребро plan_joins → clarify, S10) — thread_id сохраняется, иначе
        # второй раунд был бы непродолжаем
        llm = _StubLLM()
        runner = _StubRunner()
        runner.resume = (  # type: ignore[method-assign]
            lambda answer, *, thread_id, question=None: {
                "interrupted": True,
                "clarify_question": "Равные кандидаты: X или Y?",
            }
        )
        use_case = AnswerQuestion(runner=runner, llm=llm)
        result = use_case.resume("ответ", thread_id="t-second")
        assert result.paused is True
        assert result.thread_id == "t-second"


class _ScriptedPlanService:
    """Скриптовый планер для FSM-пути (S00-7): один исход на прогон."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.called = 0

    def plan(self, question: str, *, ctx: Any, permissions: Any) -> Any:
        self.called += 1
        return self._result


class _ScriptedGenerateService:
    """Скриптовый генератор для FSM-пути (S00-7): один исход на прогон."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.called = 0

    def generate(self, **kwargs: Any) -> Any:
        self.called += 1
        return self._result


def _valid_join_plan() -> Any:
    from text_to_sql_agent.domain.join_plan import JoinPlan, JoinPlanEdge

    return JoinPlan(
        tables=["orders", "order_items"],
        edges=[
            JoinPlanEdge(
                rel_id="e_orders_items",
                from_table="orders",
                to_table="order_items",
                from_columns=("order_id",),
                to_columns=("order_id",),
                join_type="inner",
            )
        ],
        graph_version=1,
    )


def _scripted_plan_success() -> Any:
    """PLAN-исход планера для TestRetryCycle (скелет = _GOOD_SQL/_FOREIGN_SQL)."""
    from text_to_sql_agent.application.services.plan_joins_service import (
        PlanJoinsResult,
        PlanOutcome,
    )

    return PlanJoinsResult(
        outcome=PlanOutcome.PLAN,
        plan=_valid_join_plan(),
        graph_version=1,
        llm_calls=2,
    )


class TestGenerateSqlPath:
    """S10/feat-010: содержательные узлы plan_joins → build_skeleton → generate_sql
    в FSM-пути (validate_sql/check_cost/… — заглушки-транзиты S04 до S11/S12)."""

    def _runner(
        self,
        plan_result: Any,
        generate_result: Any,
        tracer: Any | None = None,
    ) -> tuple[Any, _ScriptedPlanService, _ScriptedGenerateService]:
        from text_to_sql_agent.application.services.build_skeleton_node import (
            make_build_skeleton_node,
        )
        from text_to_sql_agent.application.services.generate_sql_node import (
            make_generate_sql_node,
        )
        from text_to_sql_agent.application.services.plan_joins_node import make_plan_joins_node

        plan_service = _ScriptedPlanService(plan_result)
        generate_service = _ScriptedGenerateService(generate_result)
        runner = build_fsm_runner(
            tracer=tracer,
            caps=CAPS,
            checkpointer=MemorySaver(),
            deadline_s=90.0,
            recursion_limit=50,
            node_overrides={
                "resolve_access": _rights_passing_node,
                "plan_joins": make_plan_joins_node(plan_service, tracer=tracer),
                "build_skeleton": make_build_skeleton_node(tracer=tracer),
                "generate_sql": make_generate_sql_node(generate_service, tracer=tracer),
            },
        )
        return runner, plan_service, generate_service

    def _scripted_plan(self, **kwargs: Any) -> Any:
        from text_to_sql_agent.application.services.plan_joins_service import (
            PlanJoinsResult,
            PlanOutcome,
        )

        defaults: dict[str, Any] = {
            "outcome": PlanOutcome.PLAN,
            "plan": _valid_join_plan(),
            "graph_version": 1,
            "llm_calls": 2,
        }
        defaults.update(kwargs)
        return PlanJoinsResult(**defaults)

    def _scripted_sql(self, **kwargs: Any) -> Any:
        from text_to_sql_agent.application.services.generate_sql_service import (
            GenerateSqlOutcome,
            GenerateSqlResult,
        )

        defaults: dict[str, Any] = {
            "outcome": GenerateSqlOutcome.SQL,
            "sql": (
                "SELECT count(*)\nFROM orders\n"
                "INNER JOIN order_items ON orders.order_id = order_items.order_id"
            ),
            "llm_calls": 1,
        }
        defaults.update(kwargs)
        return GenerateSqlResult(**defaults)

    def test_full_path_reaches_done_with_sql_and_skeleton(self) -> None:
        tracer = _RecordingTracer()
        runner, plan_service, generate_service = self._runner(
            self._scripted_plan(), self._scripted_sql(), tracer=tracer
        )
        final = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-gen")
        assert final["outcome"] == "done"
        assert final["visited_nodes"] == HAPPY_PATH
        assert final["sql"].startswith("SELECT count(*)")
        assert final["join_plan"]["tables"] == ["orders", "order_items"]
        assert final["skeleton_hash"]  # вход валидатора S11 в state (контракт стыка 1)
        assert generate_service.called == 1
        assert plan_service.called == 1

    def test_budget_counts_scripted_verdicts(self) -> None:
        # Ruling R6: узлы учитывают фактический расход (заглушка clarify платит 1,
        # планер 2, генератор 1, заглушка interpret_result — 1 (LLM-узел канона,
        # содержательный — S15) → гвард видит 5, не «бесплатные» узлы)
        runner, _, _ = self._runner(self._scripted_plan(), self._scripted_sql())
        final = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-budget")
        assert final["budget_used"]["llm_calls"] == 5

    def test_plan_fail_is_terminal_fail(self) -> None:
        # Ruling R2: FAIL планера (исчерпание M1 внутри сервиса) → terminal_fail
        from text_to_sql_agent.application.services.plan_joins_service import PlanOutcome

        runner, _, generate_service = self._runner(
            self._scripted_plan(outcome=PlanOutcome.FAIL, error="шлюз недоступен"),
            self._scripted_sql(),
        )
        final = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-pfail")
        assert final["outcome"] == "fail"
        assert final["error"]["class"] == "plan_failed"
        assert generate_service.called == 0  # до генератора прогон не дошёл

    def test_plan_no_path_is_degrade_terminal(self) -> None:
        # Ruling R2: «нет активного пути» → узел degrade (behavior feat-009);
        # заявка владельцу записана S09-сервисом, отказ формулирует degrade (S15)
        from text_to_sql_agent.application.services.plan_joins_service import PlanOutcome

        runner, _, generate_service = self._runner(
            self._scripted_plan(outcome=PlanOutcome.NO_PATH, error="активных рёбер нет"),
            self._scripted_sql(),
        )
        final = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-pnp")
        assert final["outcome"] == "degrade"
        assert "plan_joins" in final["visited_nodes"]
        assert "degrade" in final["visited_nodes"]
        assert generate_service.called == 0

    def test_generate_fail_is_degrade_terminal(self) -> None:
        # Ruling R2: отказ генератора → degrade (носитель отказа — канон degrade)
        from text_to_sql_agent.application.services.generate_sql_service import GenerateSqlOutcome

        runner, _, _ = self._runner(
            self._scripted_plan(),
            self._scripted_sql(outcome=GenerateSqlOutcome.FAIL, error="наполнение не удалось"),
        )
        final = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-gfail")
        assert final["outcome"] == "degrade"
        assert final["sql"] is None
        assert final["error"]["class"] == "generate_sql_unavailable"

    def test_skeleton_defect_from_planless_plan_degrades(self) -> None:
        # Дефект каркаса: PLAN-исход без плана → build_skeleton fail-closed → degrade
        runner, _, generate_service = self._runner(
            self._scripted_plan(plan=None), self._scripted_sql()
        )
        final = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-sdef")
        assert final["outcome"] == "degrade"
        assert final["error"]["class"] == "skeleton_defect"
        assert generate_service.called == 0

    def test_verdict_trace_events_recorded(self) -> None:
        # Ruling R6: события вердиктов узлов — факты, без SQL-текста
        tracer = _RecordingTracer()
        runner, _, _ = self._runner(self._scripted_plan(), self._scripted_sql(), tracer=tracer)
        runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-trace")
        events = [t.event for t in tracer.records]
        assert "plan_joins.verdict" in events
        assert "generate_sql.verdict" in events
        sql_events = [t for t in tracer.records if t.event == "generate_sql.verdict"]
        assert all("sql" not in t.payload for t in sql_events)  # текст SQL не трейсится


class TestRetryCycle:
    """S14/feat-014: цикл самокоррекции ADR-002 §4 внутри машины —
    generate → validate → execute → diagnose → generate.

    Реальные узлы validate_sql (S11, каталог-фейк), execute_sql (S14, фейк
    канала) и diagnose_error (S14, скрипт-сервис); plan/generate — скрипт-сервисы
    под реальными узлами (прецедент TestGenerateSqlPath).
    """

    _CATALOG = {"orders", "order_items"}
    _RIGHTS = {"orders": ["*"], "order_items": ["*"]}
    # Чужая таблица revenue — вне каталога: validate_sql → hallucination → retry
    _FOREIGN_SQL = (
        "SELECT count(*)\nFROM orders\n"
        "INNER JOIN order_items ON orders.order_id = order_items.order_id\n"
        "INNER JOIN revenue ON orders.order_id = revenue.order_id"
    )
    _GOOD_SQL = (
        "SELECT count(*)\nFROM orders\n"
        "INNER JOIN order_items ON orders.order_id = order_items.order_id"
    )

    class _QueueGenerateService:
        """Генератор-очередь: исход на каждый визит узла (FSM-ретраи)."""

        def __init__(self, results: list[Any]) -> None:
            self._results = list(results)
            self.seen_attempts: list[int] = []
            self.seen_last_sql: list[Any] = []
            self.seen_last_error: list[Any] = []

        def generate(self, **kwargs: Any) -> Any:
            if not self._results:
                raise AssertionError("генератор позван больше числа визитов сценария")
            self.seen_attempts.append(kwargs.get("start_attempt"))
            self.seen_last_sql.append(kwargs.get("last_sql"))
            self.seen_last_error.append(kwargs.get("last_error"))
            return self._results.pop(0)

    class _AlwaysRetryDiagnoseService:
        """Диагност-скрипт: класс retryable + подсказка — ретрай всегда оправдан."""

        def diagnose(self, **kwargs: Any) -> Any:
            from text_to_sql_agent.application.services.diagnose_error_service import (
                DiagnoseClass,
                DiagnoseResult,
                DiagnoseVerdict,
            )

            return DiagnoseResult(
                verdict=DiagnoseVerdict(
                    error_class=DiagnoseClass.SEMANTICS,
                    regeneration_hint="убери чужую таблицу",
                ),
                llm_calls=0,  # скрипт не тратит кап прогона (реальный диагноз — 1)
            )

    class _PassDiagnoseService:
        """Диагност-скрипт: разбор не удался — ретрай не назначается (S00-8)."""

        def diagnose(self, **kwargs: Any) -> Any:
            from text_to_sql_agent.application.services.diagnose_error_service import (
                DiagnoseResult,
            )

            return DiagnoseResult(error="шлюз недоступен", llm_calls=0)

    @staticmethod
    def _scripted_sql(sql: str) -> Any:
        from text_to_sql_agent.application.services.generate_sql_service import (
            GenerateSqlOutcome,
            GenerateSqlResult,
        )

        return GenerateSqlResult(outcome=GenerateSqlOutcome.SQL, sql=sql, llm_calls=1)

    def _runner(
        self,
        generate_results: list[Any],
        diagnose_service: Any | None = None,
        executor: Any | None = None,
        *,
        recursion_limit: int = 50,
    ) -> tuple[Any, "TestRetryCycle._QueueGenerateService"]:
        from tests.unit.fakes import FakeExecutor
        from text_to_sql_agent.application.services.build_skeleton_node import (
            make_build_skeleton_node,
        )
        from text_to_sql_agent.application.services.diagnose_error_node import (
            make_diagnose_error_node,
        )
        from text_to_sql_agent.application.services.execute_sql_node import (
            make_execute_sql_node,
        )
        from text_to_sql_agent.application.services.generate_sql_node import (
            make_generate_sql_node,
        )
        from text_to_sql_agent.application.services.plan_joins_node import make_plan_joins_node
        from text_to_sql_agent.application.services.validate_sql_node import (
            make_validate_sql_node,
        )
        from text_to_sql_agent.application.services.validate_sql_service import (
            ValidateSqlService,
        )

        generate_service = self._QueueGenerateService(generate_results)
        rights = dict(self._RIGHTS)

        def _rights_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"permissions": dict(rights), "graph_version": 1}

        runner = build_fsm_runner(
            caps=CAPS,
            checkpointer=MemorySaver(),
            deadline_s=90.0,
            recursion_limit=recursion_limit,
            node_overrides={
                "resolve_access": _rights_node,
                "plan_joins": make_plan_joins_node(_ScriptedPlanService(_scripted_plan_success())),
                "build_skeleton": make_build_skeleton_node(),
                "generate_sql": make_generate_sql_node(generate_service),
                "validate_sql": make_validate_sql_node(
                    ValidateSqlService(),
                    catalog_provider=lambda: set(self._CATALOG),
                ),
                # execute_sql — реальный узел S14: строки есть — probe не запускается
                "execute_sql": make_execute_sql_node(executor or FakeExecutor()),
                "diagnose_error": make_diagnose_error_node(
                    diagnose_service or self._AlwaysRetryDiagnoseService()
                ),
            },
        )
        return runner, generate_service

    def test_hallucination_retry_succeeds_on_attempt2(self) -> None:
        """Галлюцинация → diagnose → ретрай (попытка 2 — та же роль + ошибка) → успех."""
        runner, generate_service = self._runner(
            [self._scripted_sql(self._FOREIGN_SQL), self._scripted_sql(self._GOOD_SQL)]
        )
        final = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-cycle1")
        assert final["outcome"] == "done"
        assert generate_service.seen_attempts == [1, 2]  # ретраится генератор (M1)
        assert final["sql"] is not None and "revenue" not in final["sql"]
        # D-016: в контексте ретрая — последний SQL + разбор ошибки (фиксированный
        # текст валидатора S9 + подсказка диагностики из state.diagnosis)
        assert generate_service.seen_last_sql[1] == self._FOREIGN_SQL
        second_error = generate_service.seen_last_error[1]
        assert second_error is not None
        assert "часть данных" in second_error  # фиксированный текст S9
        assert "убери чужую таблицу" in second_error  # hint диагностики (S14)

    def test_exhaustion_yields_valid_degrade_not_infinite_loop(self) -> None:
        """Fuse ≤ 3: все попытки галлюцинируют — degrade (валидный payload), 3 визита."""
        runner, generate_service = self._runner([self._scripted_sql(self._FOREIGN_SQL)] * 5)
        final = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-cycle2")
        assert final["outcome"] == "degrade"
        assert generate_service.seen_attempts == [1, 2, 3]  # ≤ 3 попыток на узел
        assert final["error"] is not None

    def test_diagnose_unavailable_stops_without_retry(self) -> None:
        """Разбор не удался (шлюз) — ретрай не назначается, degrade (S00-8)."""
        runner, generate_service = self._runner(
            [self._scripted_sql(self._FOREIGN_SQL)],
            diagnose_service=self._PassDiagnoseService(),
        )
        final = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-cycle3")
        assert final["outcome"] == "degrade"
        assert generate_service.seen_attempts == [1]  # ретрая не было

    def test_execute_error_retries_and_succeeds(self) -> None:
        """SQL-ошибка исполнения (один раз) → diagnose → ретрай → успех — замыкание
        цикла самокоррекции ADR-002 §4 внутри машины."""

        class _FailOnceExecutor:
            """Канал: первый запрос падает по таймауту, дальше — успех (фейк S13)."""

            def __init__(self) -> None:
                self.calls = 0

            def execute(self, request: Any, *, ctx: Any) -> Any:
                from text_to_sql_agent.application.ports.sql_executor import (
                    QueryResult,
                    SQLExecutionError,
                    SQLFailureClass,
                )

                self.calls += 1
                if self.calls == 1:
                    raise SQLExecutionError("истёк statement_timeout", SQLFailureClass.TIMEOUT)
                return QueryResult(columns=["count"], rows=[{"count": 400}], row_count=1)

        runner, generate_service = self._runner(
            [self._scripted_sql(self._GOOD_SQL), self._scripted_sql(self._GOOD_SQL)],
            executor=_FailOnceExecutor(),
        )
        final = runner.run(initial_state(QUESTION, user_id="u_analyst"), thread_id="t-cycle4")
        assert final["outcome"] == "done"
        assert generate_service.seen_attempts == [1, 2]
        # В контексте ретрая — текст ошибки исполнения + подсказка диагностики
        assert generate_service.seen_last_error[1] is not None
        assert "timeout" in generate_service.seen_last_error[1]

    def test_diagnose_never_reaches_execution_directly(self) -> None:
        """Ловушка 1 плана: diagnose не имеет ребра к исполнению — путь к execute_sql
        только validate_sql → check_cost (ADR-002 §5, свойство структуры графа).

        routing возвращает имена целей литералами — аудит по коду функций: если у
        diagnose_error появится «execute_sql», тест падает раньше регрессии.
        """
        from text_to_sql_agent.orchestration.routing import (
            CONDITIONAL_ROUTES,
            route_diagnose_error,
        )

        # 1) решения diagnose_error — только ретрай-гейты и degrade (никогда execute)
        cases: list[tuple[dict[str, Any], str]] = [
            (
                {
                    "route_flags": {
                        "retry_generate_sql": True,
                        "retry_target": "generate_sql",
                    },
                    "retry_count": 0,
                },
                "retry_generate_sql",
            ),
            (
                {
                    "route_flags": {
                        "retry_generate_sql": True,
                        "retry_target": "generate_sql",
                    },
                    "retry_count": MAX_NODE_RETRIES,
                },
                "degrade",  # fuse ≤ 3 исчерпан
            ),
            ({"route_flags": {"retry_generate_step_sql": True}}, "retry_generate_step_sql"),
            ({"route_flags": {}}, "degrade"),
        ]
        for state, expected in cases:
            assert route_diagnose_error(state) == expected
        # 2) литерал «execute_sql» живёт только в route_check_cost — ребро к
        # исполнению единственное, из узла после validate_sql
        execution_sources = {
            name
            for name, route in CONDITIONAL_ROUTES.items()
            if "execute_sql" in route.__code__.co_consts
        }
        assert execution_sources == {"check_cost"}
        # 3) check_cost достижим только из validate_sql (ok-ветка)
        cost_sources = {
            name
            for name, route in CONDITIONAL_ROUTES.items()
            if "check_cost" in route.__code__.co_consts
        }
        assert cost_sources == {"validate_sql"}

    def test_broken_counters_caught_by_recursion_limit(self) -> None:
        """Второй предохранитель (ADR-002): счётчики сломаны (зацикливающий мок
        сбрасывает retry_count и бюджет каждый визит) — fuse и гвард капов не
        срабатывают, recursion_limit ловит → валидный fail (не traceback)."""

        def _looping_generate(state: dict[str, Any]) -> dict[str, Any]:
            return {
                "sql": None,
                "route_flags": {
                    "generate_sql_failed": True,
                    "generate_sql_retryable": True,
                },
                "error": {"class": "generate_sql_unavailable", "message": "битый выход"},
                "retry_count": 0,  # дефект: счётчик fuse сбрасывается каждым визитом
                "budget_used": {"llm_calls": 0, "rub": 0.0},  # и гвард капов слеп
            }

        runner = build_fsm_runner(
            caps=CAPS,
            checkpointer=MemorySaver(),
            deadline_s=90.0,
            recursion_limit=25,
            node_overrides={
                "resolve_access": lambda state: {"permissions": {}, "graph_version": 0},
                "generate_sql": _looping_generate,
            },
        )
        final = runner.run(initial_state(QUESTION), thread_id="t-loop")
        assert final["outcome"] == "fail"
        assert final["error"]["class"] == RECURSION_LIMIT_ERROR_CLASS
