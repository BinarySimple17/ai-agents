"""Composition root — единственное место сборки зависимостей (AGENTS.md Boundaries).

Подмена на фейк в тестах — через этот же корень: тесты строят свои `Settings`
и передают их в `build_container`, либо собирают компоненты напрямую
(юнит-тесты адаптера инжектируют мок-клиент в `OpenAIProvider`).
"""

from dataclasses import dataclass

from text_to_sql_agent.application.ports.llm_provider import LLMProvider
from text_to_sql_agent.application.ports.tracer import Tracer
from text_to_sql_agent.application.services.run_sql_tool import RunSqlTool
from text_to_sql_agent.config.settings import Settings, load_settings
from text_to_sql_agent.infrastructure.graph.neo4j_graph_store import Neo4jGraphStore
from text_to_sql_agent.infrastructure.llm.openai_provider import OpenAIProvider
from text_to_sql_agent.infrastructure.memory.pgvector_store import PgVectorStore
from text_to_sql_agent.infrastructure.observability.composite_tracer import TeeTracer
from text_to_sql_agent.infrastructure.observability.console_tracer import ConsoleTracer
from text_to_sql_agent.infrastructure.observability.jsonl_tracer import JsonlTracer
from text_to_sql_agent.infrastructure.persistence.postgres_executor import PostgresExecutor


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


def build_container(
    settings: Settings | None = None,
    *,
    console_trace: bool = False,
    knowledge_writer: bool = False,
) -> Container:
    """Дефолтная сборка; `settings=None` → fail-fast чтение окружения.

    `console_trace=True` — этапы работы агента дублируются в stderr (ConsoleTracer,
    BL-004/D-022); JSONL-персистентность включена всегда. `knowledge_writer=True` —
    только офлайн-индексатор (make index-knowledge): runtime-контур не держит
    сервисный DSN записи в память знаний (ruling W1, ревью S06 Important-2).
    """
    settings = settings or load_settings()
    tracer: Tracer = JsonlTracer(settings.trace_jsonl_path)
    if console_trace:
        tracer = TeeTracer(tracer, ConsoleTracer())
    llm = OpenAIProvider(settings, tracer=tracer)
    # Канал SQL — строго AGENT_RO_DSN (security §2.1); сервисный POSTGRES_DSN — не здесь.
    executor = PostgresExecutor(settings.agent_ro_dsn.get_secret_value())
    run_sql_tool = RunSqlTool(executor)
    # Векторная память (S06): поиск — agent_planner (c4-l2 v1.8 «пул agent_planner:
    # retrieval»); write_dsn (сервисный) — только офлайн-индексация
    # make index-knowledge; runtime-роли в память знаний не пишут (ruling W1).
    vector_store = PgVectorStore(
        settings.agent_planner_dsn,
        write_dsn=settings.postgres_dsn.get_secret_value() if knowledge_writer else None,
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
    return Container(
        settings=settings,
        llm=llm,
        tracer=tracer,
        executor=executor,
        run_sql_tool=run_sql_tool,
        vector_store=vector_store,
        graph_store=graph_store,
    )
