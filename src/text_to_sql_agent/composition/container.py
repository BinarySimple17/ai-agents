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
from text_to_sql_agent.infrastructure.llm.openai_provider import OpenAIProvider
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


def build_container(settings: Settings | None = None, *, console_trace: bool = False) -> Container:
    """Дефолтная сборка; `settings=None` → fail-fast чтение окружения.

    `console_trace=True` — этапы работы агента дублируются в stderr (ConsoleTracer,
    BL-004/D-022); JSONL-персистентность включена всегда.
    """
    settings = settings or load_settings()
    tracer: Tracer = JsonlTracer(settings.trace_jsonl_path)
    if console_trace:
        tracer = TeeTracer(tracer, ConsoleTracer())
    llm = OpenAIProvider(settings, tracer=tracer)
    # Канал SQL — строго AGENT_RO_DSN (security §2.1); сервисный POSTGRES_DSN — не здесь.
    executor = PostgresExecutor(settings.agent_ro_dsn.get_secret_value())
    run_sql_tool = RunSqlTool(executor)
    return Container(
        settings=settings, llm=llm, tracer=tracer, executor=executor, run_sql_tool=run_sql_tool
    )
