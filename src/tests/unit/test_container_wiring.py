"""Проверка wiring контейнера (S06, ревью-2): дефолтный runtime-контур не держит
write_dsn памяти знаний (ruling W1 ARCH-GATE S06, ревью I2 — периметр прав);
writer — только офлайн-индексатор с флагом knowledge_writer=True.

build_container строит реальные компоненты, но ни один не открывает соединений
на сборке (ленивые клиенты/DSN-строки) — тест без сети и БД.
"""

import pytest
from langgraph.checkpoint.memory import MemorySaver
from pydantic import SecretStr

from tests.unit.fakes import FakeExecutor
from text_to_sql_agent.application.ports.auth_provider import AuthenticationError
from text_to_sql_agent.application.ports.permission_store import UserRights
from text_to_sql_agent.application.ports.vector_store import DocKind, KnowledgeDocument
from text_to_sql_agent.composition.container import build_container
from text_to_sql_agent.config.settings import Settings

_DOC = KnowledgeDocument(doc_kind=DocKind.DDL, content="x", model_version="m", source_ref="s")
_VEC = [0.01] * 1024


def _mem_ckpt() -> MemorySaver:
    # Юнит-контур без инфры (S00 правило 7): сессии interrupt — MemorySaver;
    # живой Valkey — integration (test_clarify_resume)
    return MemorySaver()


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        llm_api_key=SecretStr("sk-test"),
        model_cheap="cheap",
        model_mid="mid",
        model_strong="strong",
        budget_per_request_rub=2.0,
        model_cheap_price_in=0,
        model_cheap_price_out=0,
        model_mid_price_in=0,
        model_mid_price_out=0,
        model_strong_price_in=0,
        model_strong_price_out=0,
        embedding_model="embed-1",
        postgres_dsn="postgresql://postgres:pw@127.0.0.1:5432/agent",
        agent_ro_dsn="postgresql://agent_ro:pw@127.0.0.1:5432/agent",
        agent_trace_dsn="postgresql://agent_trace:pw@127.0.0.1:5432/agent",
        valkey_url="redis://127.0.0.1:6379/0",
        neo4j_uri="bolt://127.0.0.1:7687",
        neo4j_user="neo4j",
        neo4j_password=SecretStr("pw"),
        agent_ro_password=SecretStr("agent_ro"),
        agent_planner_password=SecretStr("agent_planner"),
        agent_trace_password=SecretStr("agent_trace"),
        registry_writer_password=SecretStr("registry_writer"),
        trace_jsonl_path=tmp_path / "trace.jsonl",
    )


def test_default_container_cannot_write_knowledge(tmp_path) -> None:
    # Runtime-контур (CLI/FSM) не держит сервисный DSN записи в память знаний
    container = build_container(_settings(tmp_path), checkpointer=_mem_ckpt())
    with pytest.raises(RuntimeError, match="write_dsn"):
        container.vector_store.upsert([_DOC], [_VEC])


def test_knowledge_writer_container_has_write_dsn(tmp_path) -> None:
    # Офлайн-индексатор собирает контейнер с write-каналом (make index-knowledge)
    settings = _settings(tmp_path)
    container = build_container(settings, knowledge_writer=True, checkpointer=_mem_ckpt())
    assert (
        container.vector_store._write_dsn  # noqa: SLF001 — инвариант ruling W1
        == settings.postgres_dsn.get_secret_value()
    )


def test_plan_joins_budget_shlew_wired_to_adapter(tmp_path) -> None:
    # Шов бюджета узла plan_joins (ruling Q7 ARCH-GATE S09): остаток =
    # кап запроса − резидентный аккумулятор адаптера; на сборке аккумулятор 0
    container = build_container(_settings(tmp_path), checkpointer=_mem_ckpt())
    assert type(container.plan_joins_service).__name__ == "PlanJoinsService"
    assert container.plan_joins_service._budget_remaining_roubles is not None  # noqa: SLF001
    assert container.plan_joins_service._budget_remaining_roubles() == pytest.approx(2.0)  # noqa: SLF001
    assert container.edge_proposal_store is not None  # очередь заявок собрана (миграция 0009)


def test_generate_sql_budget_seam_wired_to_adapter(tmp_path) -> None:
    # Шов бюджета узла generate_sql (ruling Q7 — тот же, что plan_joins/clarify):
    # эскалация mid→strong проверяет остаток = кап − аккумулятор адаптера
    # (ревью S10 minor — без wiring-теста регресс молча отключил бы резерв)
    container = build_container(_settings(tmp_path), checkpointer=_mem_ckpt())
    assert type(container.generate_sql_service).__name__ == "GenerateSqlService"
    assert container.generate_sql_service._budget_remaining_roubles is not None  # noqa: SLF001
    assert container.generate_sql_service._budget_remaining_roubles() == pytest.approx(2.0)  # noqa: SLF001


def test_budget_guard_wired_as_port(tmp_path) -> None:
    # S15/feat-015 (ruling R4 ARCH-GATE): шов ruling Q7 переведён на порт
    # BudgetGuard — read-side снапшот над аккумулятором S03 (BL-015 закрыт);
    # сервисы получают bound method с той же сигнатурой Callable[[], float]
    from text_to_sql_agent.application.ports.budget_guard import BudgetGuard

    container = build_container(_settings(tmp_path), checkpointer=_mem_ckpt())
    assert isinstance(container.budget_guard, BudgetGuard)
    assert container.budget_guard.remaining_rub() == pytest.approx(
        container.settings.budget_per_request_rub
    )
    assert container.budget_guard.spent_rub() == pytest.approx(0.0)  # сборка — без вызовов
    # шов сервисов — порт (одна точка источника, не лямбды-копии); bound methods
    # сравниваются по == (каждый доступ создаёт новый объект-обёртку)
    seam = container.budget_guard.remaining_rub
    for service in (
        container.plan_joins_service,
        container.generate_sql_service,
        container.clarify_service,
        container.diagnose_service,
    ):
        assert service._budget_remaining_roubles == seam  # noqa: SLF001


def test_build_container_respects_rrf_alpha_dense(tmp_path) -> None:
    # I3 ревью S26: измерительный контур (bench dense-only α=1.0) собирается
    # ТОЛЬКО через build_container — сборка адаптера вне composition запрещена
    container = build_container(_settings(tmp_path), rrf_alpha_dense=1.0)
    assert container.vector_store._rrf_alpha_dense == 1.0  # noqa: SLF001 — инвариант сборки


class _FakePermissionStore:
    """Минимальный PermissionStore для юнит-wiring FSM: known user с правами
    на две таблицы скриптового плана (S11: валидатор сверяет ACL + каталог мира)."""

    def __init__(self, *, known: bool = True) -> None:
        self._known = known

    def settings_versions(self) -> dict[str, int]:
        return {"permissions": 1, "user_roles": 1}

    def fetch_rights(self, user_id: str) -> UserRights:
        return UserRights(
            user_id=user_id,
            user_known=self._known,
            roles=("analyst",) if self._known else (),
            tables={"orders": ("*",), "order_items": ("*",)} if self._known else {},
            versions={"permissions": 1, "user_roles": 1},
        )

    def data_catalog(self) -> frozenset[str]:
        return frozenset({"orders", "order_items", "customers", "campaigns", "products"})


def test_fsm_runner_wired_from_settings(tmp_path) -> None:
    # S04/feat-004: FSM-каркас собирается в composition, раннер соответствует
    # engine-neutral порту GraphRunner (ruling R2 ARCH-GATE S04)
    from text_to_sql_agent.application.ports.graph_runner import GraphRunner

    settings = _settings(tmp_path)
    container = build_container(settings, checkpointer=_mem_ckpt())
    assert isinstance(container.fsm_runner, GraphRunner)
    assert container.fsm_runner._deadline_s == settings.fsm_deadline_s  # noqa: SLF001
    assert container.fsm_runner._recursion_limit == settings.fsm_recursion_limit  # noqa: SLF001


def test_fsm_runner_honors_caps_and_completes(tmp_path) -> None:
    # Гвард глобальных капов — из Settings (≤ 8 вызовов / ≤ 2 ₽), не моделью:
    # исчерпанный счётчик вызовов → budget_stop ДО первого LLM-узла.
    # user_id обязателен (S05): узел resolve_access fail-closed на пустой
    # identity → rejected сработал бы раньше гварда (решение ребра первично).
    # clarify/plan_joins/generate_sql — скриптовые сервисы (S00 правило 7:
    # реальные узлы зовут LLM, в юните — только инъекция через composition)
    from text_to_sql_agent.application.services.clarify_service import (
        ClarifyOutcome,
        ClarifyResult,
    )
    from text_to_sql_agent.application.services.generate_sql_service import (
        GenerateSqlOutcome,
        GenerateSqlResult,
    )
    from text_to_sql_agent.application.services.plan_joins_service import (
        PlanJoinsResult,
        PlanOutcome,
    )
    from text_to_sql_agent.application.services.stub_nodes import initial_state
    from text_to_sql_agent.domain.join_plan import JoinPlan, JoinPlanEdge

    class _ScriptedClarify:
        def clarify(self, question: str, *, ctx: object, permissions: object) -> ClarifyResult:
            return ClarifyResult(outcome=ClarifyOutcome.UNAMBIGUOUS)

    class _ScriptedPlanJoins:
        def plan(self, question: str, *, ctx: object, permissions: object) -> PlanJoinsResult:
            return PlanJoinsResult(
                outcome=PlanOutcome.PLAN,
                plan=JoinPlan(
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
                ),
            )

    class _ScriptedGenerateSql:
        # S11: SQL скрипта соответствует скелету плана — валидатор (реальный
        # узел) сверяет структуру/ACL и пропускает happy path до done
        def generate(self, **kwargs: object) -> GenerateSqlResult:
            return GenerateSqlResult(
                outcome=GenerateSqlOutcome.SQL,
                sql=(
                    "SELECT count(*) FROM orders o INNER JOIN order_items i "
                    "ON o.order_id = i.order_id"
                ),
            )

    container = build_container(
        _settings(tmp_path),
        permission_store=_FakePermissionStore(),
        checkpointer=_mem_ckpt(),
        clarify_service=_ScriptedClarify(),  # type: ignore[arg-type]
        plan_joins_service=_ScriptedPlanJoins(),  # type: ignore[arg-type]
        generate_sql_service=_ScriptedGenerateSql(),  # type: ignore[arg-type]
        executor=FakeExecutor(),  # S14: execute_sql — реальный узел FSM (фейк без БД)
    )
    state = initial_state("Сколько заказов?", user_id="u_analyst")
    state["budget_used"] = {"llm_calls": container.settings.fsm_max_llm_calls, "rub": 0.0}
    assert container.fsm_runner.run(state)["outcome"] == "budget_stop"
    # Нейтральный прогон — happy path до терминала done; resolve_access отдал
    # права из стора в state (S00 контракт 3), validate_sql (S11, реальный узел)
    # пропустил скриптовый SQL (структура = план, таблицы в правах) и положил
    # вердикт в state.validation; sql перезаписан исполненным текстом (R1: LIMIT);
    # execute_sql (S14, реальный узел) исполнил SQL фейком канала (строки есть —
    # probe не запускается) → compact_result → done
    result = container.fsm_runner.run(initial_state("Сколько заказов?", user_id="u_analyst"))
    assert result["outcome"] == "done"
    assert result["permissions"] == {"orders": ["*"], "order_items": ["*"]}
    assert result["validation"]["outcome"] == "ok"
    assert "LIMIT" in result["sql"].upper()
    assert result["row_count"] == 1


def test_fsm_rejects_unknown_user_without_llm_calls(tmp_path) -> None:
    # Fail-closed (ruling R6 ARCH-GATE S05): unknown user_id → rejected, БЕЗ
    # единого LLM-вызова (узел до первого LLM-узла clarify) — behavior feat-005
    from text_to_sql_agent.application.services.stub_nodes import initial_state

    container = build_container(
        _settings(tmp_path),
        permission_store=_FakePermissionStore(known=False),
        checkpointer=_mem_ckpt(),
    )
    result = container.fsm_runner.run(initial_state("Сколько заказов?", user_id="ghost"))
    assert result["outcome"] == "rejected"
    assert result["budget_used"]["llm_calls"] == 0
    assert result["error"]["class"] == "unknown_user"


def test_auth_provider_fail_closed_without_tokens(tmp_path) -> None:
    # AGENT_DEV_TOKENS пуст → DevTokenAuth отклоняет любой токен (ruling R4:
    # дефолтного пользователя нет)
    container = build_container(_settings(tmp_path), checkpointer=_mem_ckpt())
    with pytest.raises(AuthenticationError):
        container.auth_provider.verify("anything")


# --- S15/feat-015 Task 5: терминалы done/degrade/budget_stop на реальном графе ---


class _ScriptedInterpret:
    """Двойник InterpretService: фиксированный текст + запись kwargs узла."""

    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def interpret(self, **kwargs):
        from text_to_sql_agent.application.services.interpret_result_service import (
            InterpretOutcome,
            InterpretResult,
        )

        self.kwargs = kwargs
        return InterpretResult(outcome=InterpretOutcome.ANSWER, text="Итого 1 заказ.", llm_calls=1)


class _ScriptedDegrade:
    """Двойник DegradeService: фиксированный текст отказа."""

    def formulate(self, *, case):
        from text_to_sql_agent.application.services.degrade_service import DegradeResult

        return DegradeResult(text="Отказ сформулирован.", used_llm=False, llm_calls=0, error=None)


def test_full_happy_path_yields_final_answer(tmp_path) -> None:
    # Успех (критерий приёмки feat-015): … → compact_result → interpret_result →
    # done с финальным payload (текст + SQL + допущения/usage_notes + флаги + ₽);
    # компакт/interpret/degrade — реальные узлы, LLM — инъекции (S00 правило 7)
    from text_to_sql_agent.application.services.clarify_service import (
        ClarifyOutcome,
        ClarifyResult,
    )
    from text_to_sql_agent.application.services.generate_sql_service import (
        GenerateSqlOutcome,
        GenerateSqlResult,
    )
    from text_to_sql_agent.application.services.plan_joins_service import (
        PlanJoinsResult,
        PlanOutcome,
    )
    from text_to_sql_agent.application.services.stub_nodes import initial_state
    from text_to_sql_agent.domain.join_plan import JoinPlan, JoinPlanEdge

    class _Clarify:
        def clarify(self, question, *, ctx, permissions):
            return ClarifyResult(outcome=ClarifyOutcome.UNAMBIGUOUS)

    class _PlanJoins:
        def plan(self, question, *, ctx, permissions):
            return PlanJoinsResult(
                outcome=PlanOutcome.PLAN,
                plan=JoinPlan(
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
                    assumptions=["замечание связи от реестра"],
                ),
            )

    class _Generate:
        def generate(self, **kwargs):
            return GenerateSqlResult(
                outcome=GenerateSqlOutcome.SQL,
                sql=(
                    "SELECT count(*) FROM orders o INNER JOIN order_items i "
                    "ON o.order_id = i.order_id"
                ),
            )

    interpret = _ScriptedInterpret()
    container = build_container(
        _settings(tmp_path),
        permission_store=_FakePermissionStore(),
        checkpointer=_mem_ckpt(),
        clarify_service=_Clarify(),  # type: ignore[arg-type]
        plan_joins_service=_PlanJoins(),  # type: ignore[arg-type]
        generate_sql_service=_Generate(),  # type: ignore[arg-type]
        interpret_service=interpret,  # type: ignore[arg-type]
        executor=FakeExecutor(),
    )
    result = container.fsm_runner.run(initial_state("Сколько заказов?", user_id="u_analyst"))
    assert result["outcome"] == "done"
    answer = result["final_answer"]
    assert answer["text"] == "Итого 1 заказ."
    assert "LIMIT" in answer["sql"].upper()  # исполненный текст (R1), не исходный
    assert answer["assumptions"] == []  # clarify-допущений не было
    assert answer["usage_notes"] == ["замечание связи от реестра"]  # JoinPlan → payload
    assert answer["row_count"] == 1
    assert answer["truncated"] is False and answer["approximate"] is False
    assert answer["incomplete"] is False
    assert answer["cost_rub"] == pytest.approx(0.0)  # снапшот guard'а (R6), не None
    assert result["budget_used"]["llm_calls"] == 1
    # сервис интерпретатора получил компакт, не сырые строки (behavior feat-015)
    assert interpret.kwargs["compact"]["row_count"] == 1


def test_acl_denied_ends_in_degrade_with_final_answer(tmp_path) -> None:
    # Отказ по правам (критерий feat-015): SQL из чужой зоны → validate_sql
    # denied → degrade (без retry) → терминал degrade с final_answer
    from text_to_sql_agent.application.services.clarify_service import (
        ClarifyOutcome,
        ClarifyResult,
    )
    from text_to_sql_agent.application.services.generate_sql_service import (
        GenerateSqlOutcome,
        GenerateSqlResult,
    )
    from text_to_sql_agent.application.services.plan_joins_service import (
        PlanJoinsResult,
        PlanOutcome,
    )
    from text_to_sql_agent.application.services.stub_nodes import initial_state
    from text_to_sql_agent.domain.join_plan import JoinPlan, JoinPlanEdge

    class _Clarify:
        def clarify(self, question, *, ctx, permissions):
            return ClarifyResult(outcome=ClarifyOutcome.UNAMBIGUOUS)

    class _PlanJoins:
        def plan(self, question, *, ctx, permissions):
            return PlanJoinsResult(
                outcome=PlanOutcome.PLAN,
                plan=JoinPlan(
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
                ),
            )

    class _GenerateForeignZone:
        # SQL трогает customers (в каталоге мира, вне прав analyst) — denied
        def generate(self, **kwargs):
            return GenerateSqlResult(
                outcome=GenerateSqlOutcome.SQL,
                sql=(
                    "SELECT count(*) FROM orders o INNER JOIN customers c "
                    "ON o.customer_id = c.customer_id"
                ),
            )

    container = build_container(
        _settings(tmp_path),
        permission_store=_FakePermissionStore(),
        checkpointer=_mem_ckpt(),
        clarify_service=_Clarify(),  # type: ignore[arg-type]
        plan_joins_service=_PlanJoins(),  # type: ignore[arg-type]
        generate_sql_service=_GenerateForeignZone(),  # type: ignore[arg-type]
        degrade_service=_ScriptedDegrade(),  # type: ignore[arg-type]
        executor=FakeExecutor(),
    )
    result = container.fsm_runner.run(initial_state("Покажи заказы клиентов", user_id="u_analyst"))
    assert result["outcome"] == "degrade"
    assert "degrade" in result["visited_nodes"]
    assert result["validation"]["outcome"] == "denied"
    answer = result["final_answer"]
    assert answer["text"] == "Отказ сформулирован."
    assert answer["cause"] == "rights"
    assert answer["sql"] is None  # SQL недоступной выборки не показывается (S9)
    assert result["incomplete"] is False  # деградация — валидный исход, не неполный


def test_budget_stop_guard_sets_incomplete_in_state(tmp_path) -> None:
    # R3: гвард капов перехватывает НА РЕБРЕ — терминал budget_stop ставит
    # incomplete в state (частичные данные не выдаются за полное)
    from text_to_sql_agent.application.services.stub_nodes import initial_state

    container = build_container(
        _settings(tmp_path),
        permission_store=_FakePermissionStore(),
        checkpointer=_mem_ckpt(),
        executor=FakeExecutor(),
    )
    state = initial_state("Сколько заказов?", user_id="u_analyst")
    state["budget_used"] = {"llm_calls": 0, "rub": container.settings.budget_per_request_rub}
    result = container.fsm_runner.run(state)
    assert result["outcome"] == "budget_stop"
    assert result["incomplete"] is True


def test_run_sql_tool_wired_behind_single_gate(tmp_path) -> None:
    # S31 (аудит A1/E1, R1–R3): тул собран за ЕДИНЫМ рубежом — ТЕ ЖЕ экземпляры,
    # что у FSM-ребра validate_sql (один валидатор/права/граф, не копии)
    container = build_container(_settings(tmp_path), checkpointer=_mem_ckpt())
    assert container.run_sql_tool._validator is container.validate_sql_service  # noqa: SLF001
    assert container.run_sql_tool._graph_store is container.graph_store  # noqa: SLF001
    assert container.run_sql_tool._tracer is container.tracer  # noqa: SLF001
    assert container.run_sql_tool._permission_store is container.resolve_access_service._store  # noqa: SLF001
