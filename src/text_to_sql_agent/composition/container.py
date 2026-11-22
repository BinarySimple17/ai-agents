"""Composition root — единственное место сборки зависимостей (AGENTS.md Boundaries).

Подмена на фейк в тестах — через этот же корень: тесты строят свои `Settings`
и передают их в `build_container`, либо собирают компоненты напрямую
(юнит-тесты адаптера инжектируют мок-клиент в `OpenAIProvider`).
"""

from dataclasses import dataclass
from typing import Any

from text_to_sql_agent.application.ports.auth_provider import AuthProvider
from text_to_sql_agent.application.ports.llm_provider import LLMProvider
from text_to_sql_agent.application.ports.permission_store import PermissionStore
from text_to_sql_agent.application.ports.tracer import Tracer
from text_to_sql_agent.application.services.build_skeleton_node import make_build_skeleton_node
from text_to_sql_agent.application.services.clarify_node import make_clarify_node
from text_to_sql_agent.application.services.clarify_service import ClarifyService
from text_to_sql_agent.application.services.generate_sql_node import make_generate_sql_node
from text_to_sql_agent.application.services.generate_sql_service import GenerateSqlService
from text_to_sql_agent.application.services.plan_joins_node import make_plan_joins_node
from text_to_sql_agent.application.services.plan_joins_service import PlanJoinsService
from text_to_sql_agent.application.services.resolve_access_node import make_resolve_access_node
from text_to_sql_agent.application.services.resolve_access_service import ResolveAccessService
from text_to_sql_agent.application.services.run_sql_tool import RunSqlTool
from text_to_sql_agent.application.use_cases.answer_question import AnswerQuestion
from text_to_sql_agent.config.settings import Settings, load_settings
from text_to_sql_agent.infrastructure.cache.valkey_checkpointer import ValkeyCheckpointer
from text_to_sql_agent.infrastructure.graph.neo4j_graph_store import Neo4jGraphStore
from text_to_sql_agent.infrastructure.llm.openai_provider import OpenAIProvider
from text_to_sql_agent.infrastructure.memory.pgvector_store import PgVectorStore
from text_to_sql_agent.infrastructure.observability.composite_tracer import TeeTracer
from text_to_sql_agent.infrastructure.observability.console_tracer import ConsoleTracer
from text_to_sql_agent.infrastructure.observability.jsonl_tracer import JsonlTracer
from text_to_sql_agent.infrastructure.persistence.dev_token_auth import DevTokenAuth
from text_to_sql_agent.infrastructure.persistence.edge_proposal_store import PgEdgeProposalStore
from text_to_sql_agent.infrastructure.persistence.pg_permission_store import PgPermissionStore
from text_to_sql_agent.infrastructure.persistence.postgres_executor import PostgresExecutor
from text_to_sql_agent.orchestration.interrupt_gate import LangGraphInterruptGate
from text_to_sql_agent.orchestration.routing import FSMGuardCaps
from text_to_sql_agent.orchestration.runner import LangGraphRunner, build_fsm_runner


@dataclass(slots=True)
class Container:
    """Собранные зависимости приложения (наполняется по мере сессий)."""

    settings: Settings
    llm: LLMProvider
    tracer: Tracer
    executor: PostgresExecutor
    run_sql_tool: RunSqlTool
    vector_store: PgVectorStore
    graph_store: Neo4jGraphStore
    edge_proposal_store: PgEdgeProposalStore
    plan_joins_service: PlanJoinsService
    generate_sql_service: GenerateSqlService
    clarify_service: ClarifyService
    auth_provider: AuthProvider
    resolve_access_service: ResolveAccessService
    # Сессии interrupt (S08, ruling R2): ValkeyCheckpointer в проде;
    # MemorySaver/фейк в тестах — контракт сейвера задан LangGraph (Any)
    checkpointer: Any
    fsm_runner: LangGraphRunner
    answer_question: AnswerQuestion


def build_container(
    settings: Settings | None = None,
    *,
    console_trace: bool = False,
    knowledge_writer: bool = False,
    rrf_alpha_dense: float | None = None,
    permission_store: PermissionStore | None = None,
    checkpointer: object | None = None,
    clarify_service: ClarifyService | None = None,
    plan_joins_service: PlanJoinsService | None = None,
    generate_sql_service: GenerateSqlService | None = None,
) -> Container:
    """Дефолтная сборка; `settings=None` → fail-fast чтение окружения.

    `console_trace=True` — этапы работы агента дублируются в stderr (ConsoleTracer,
    BL-004/D-022); JSONL-персистентность включена всегда. `knowledge_writer=True` —
    только офлайн-индексатор (make index-knowledge): runtime-контур не держит
    сервисный DSN записи в память знаний (ruling W1, ревью S06 Important-2).
    `checkpointer` — сессии interrupt-механики (S08/feat-008, ruling R2 ARCH-GATE):
    None → ValkeyCheckpointer (TTL из Settings); инъекция фейка — юнит-тесты без
    инфры (S00 правило 7, прецедент permission_store). `clarify_service` /
    `plan_joins_service` / `generate_sql_service` — инъекции скриптовых сервисов
    юнит-тестов wiring и integration-прогонов без LLM (S00 правило 7 — LLM в
    тестах только моки; реальный контур собирает сервисы со швом бюджета Q7).
    """
    settings = settings or load_settings()
    tracer: Tracer = JsonlTracer(settings.trace_jsonl_path)
    if console_trace:
        tracer = TeeTracer(tracer, ConsoleTracer())
    llm = OpenAIProvider(settings, tracer=tracer)
    # Канал SQL — строго AGENT_RO_DSN (security §2.1); сервисный POSTGRES_DSN — не здесь.
    executor = PostgresExecutor(settings.agent_ro_dsn.get_secret_value())
    run_sql_tool = RunSqlTool(executor)
    # Векторная память (S06/S26): поиск — agent_planner (c4-l2 v1.8 «пул agent_planner:
    # retrieval»); write_dsn (сервисный) — только офлайн-индексация
    # make index-knowledge; runtime-роли в память знаний не пишут (ruling W1).
    # expected_model_version (BL-009 M3): смена эмбеддера до пересборки — строки
    # чужого векторного пространства не смешиваются с текущим (Task 5 S26).
    # rrf_alpha_dense — вес dense-ноги RRF: None = канон D-026 (0.9); явное
    # значение — для измерительных контуров (bench: α=1.0 = pure dense, I3 ревью —
    # адаптер собирается ТОЛЬКО здесь, не в apps)
    vector_store_kwargs = (
        {}
        if rrf_alpha_dense is None
        else {
            "rrf_alpha_dense": rrf_alpha_dense,
        }
    )
    vector_store = PgVectorStore(
        settings.agent_planner_dsn,
        write_dsn=settings.postgres_dsn.get_secret_value() if knowledge_writer else None,
        expected_model_version=settings.embedding_model,
        **vector_store_kwargs,
    )
    # Графовая память (S07): Neo4j — только топология, реестр/семантика — PG.
    # Чтение реестра — agent_planner (BL-003 п.3: GRANT SELECT на вьюху active
    # из миграции 0002; registry_writer без SELECT не трогается, роль синка
    # не вводится — наименьшие привилегии, один read-путь с рантаймом).
    graph_store = Neo4jGraphStore(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password.get_secret_value(),
        planner_dsn=settings.agent_planner_dsn,
    )
    # Очередь заявок (S09 Task 4): запись — INSERT-only под agent_planner
    # (миграция 0009, ruling Q1 ARCH-GATE S09); append-only через отсутствие
    # UPDATE/DELETE/SELECT-грантов.
    edge_proposal_store = PgEdgeProposalStore(settings.agent_planner_dsn)
    # Узел plan_joins (S09 сервис, подключение к FSM — S10/feat-010): retrieval +
    # граф + ранжирование. Шов бюджета (ruling Q7): остаток = кап запроса −
    # резидентный аккумулятор адаптера; FSM (V4) заменит на порт BudgetGuard.
    plan_joins_service = plan_joins_service or PlanJoinsService(
        llm=llm,
        vector_store=vector_store,
        graph_store=graph_store,
        edge_proposals=edge_proposal_store,
        budget_remaining_roubles=lambda: settings.budget_per_request_rub - llm.budget_used_rub,
    )
    # Узел generate_sql (S10/feat-010): наполнение скелета deepseek/mid; ретраи
    # M1 с эскалацией mid→strong при остатке с резервом (ruling R7 ARCH-GATE —
    # тот же шов бюджета, что у plan_joins/clarify)
    generate_sql_service = generate_sql_service or GenerateSqlService(
        llm=llm,
        vector_store=vector_store,
        budget_remaining_roubles=lambda: settings.budget_per_request_rub - llm.budget_used_rub,
    )
    # Права и identity (S05/feat-005): чтение настроек — agent_planner (гранты
    # SELECT 0002:80-83, ruling R1 ARCH-GATE S05); инъекция фейка — юнит-тесты
    # wiring (прецедент rrf_alpha_dense). Identity — DevTokenAuth из Settings
    # (ruling R4: verify в точке входа, сырой user_id не проходит).
    permission_store = permission_store or PgPermissionStore(settings.agent_planner_dsn)
    resolve_access_service = ResolveAccessService(permission_store)
    auth_provider: AuthProvider = DevTokenAuth(settings.dev_token_map)
    # Узел clarify (S08/feat-008): классификация однозначности + interrupt-
    # механика. Шов бюджета — ruling Q7 (тот же, что у plan_joins: остаток =
    # кап запроса − резидентный аккумулятор адаптера; FSM замена — S15 BudgetGuard).
    clarify_service = clarify_service or ClarifyService(
        llm=llm,
        vector_store=vector_store,
        budget_remaining_roubles=lambda: settings.budget_per_request_rub - llm.budget_used_rub,
    )
    # Сессии interrupt (S08, ruling R2 ARCH-GATE): Valkey + TTL из Settings;
    # инъекция фейка/MemorySaver — юнит-тесты без инфры (S00 правило 7)
    checkpointer = checkpointer or ValkeyCheckpointer(
        settings.valkey_url, ttl_s=settings.clarify_session_ttl_s
    )
    # FSM-каркас (S04/feat-004): граф на заглушках-транзитах; содержательные
    # сервисы подменяются в последующих сессиях вехи V4 (S05/S08/S10/S11/S14/S15)
    # — сборка остаётся здесь (composition — единственное место сборки). Гвард
    # глобальных капов — из Settings (≤ 8 вызовов / ≤ 2 ₽), дедлайн и
    # recursion_limit раннер берёт из Settings (комментарии «почему» — там же).
    fsm_runner = build_fsm_runner(
        tracer=tracer,
        caps=FSMGuardCaps(
            max_llm_calls=settings.fsm_max_llm_calls,
            budget_rub=settings.budget_per_request_rub,
        ),
        checkpointer=checkpointer,
        deadline_s=settings.fsm_deadline_s,
        recursion_limit=settings.fsm_recursion_limit,
        # S05/feat-005: resolve_access — содержательный узел (права из настроек);
        # S08/feat-008: clarify — классификация + interrupt (шлюз-адаптер —
        # orchestration, ruling R2); S10/feat-010: plan_joins (сервис S09) →
        # build_skeleton (код) → generate_sql (наполнение mid); заглушки остаются
        # для юнит-тестов каркаса (test_graph строит граф сам)
        node_overrides={
            "resolve_access": make_resolve_access_node(resolve_access_service, tracer=tracer),
            "clarify": make_clarify_node(clarify_service, LangGraphInterruptGate(), tracer=tracer),
            "plan_joins": make_plan_joins_node(plan_joins_service, tracer=tracer),
            "build_skeleton": make_build_skeleton_node(tracer=tracer),
            "generate_sql": make_generate_sql_node(generate_sql_service, tracer=tracer),
        },
    )
    answer_question = AnswerQuestion(runner=fsm_runner, llm=llm)
    return Container(
        settings=settings,
        llm=llm,
        tracer=tracer,
        executor=executor,
        run_sql_tool=run_sql_tool,
        vector_store=vector_store,
        graph_store=graph_store,
        edge_proposal_store=edge_proposal_store,
        plan_joins_service=plan_joins_service,
        generate_sql_service=generate_sql_service,
        clarify_service=clarify_service,
        auth_provider=auth_provider,
        resolve_access_service=resolve_access_service,
        checkpointer=checkpointer,
        fsm_runner=fsm_runner,
        answer_question=answer_question,
    )
