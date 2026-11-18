"""Composition root — единственное место сборки зависимостей (AGENTS.md Boundaries).

Подмена на фейк в тестах — через этот же корень: тесты строят свои `Settings`
и передают их в `build_container`, либо собирают компоненты напрямую
(юнит-тесты адаптера инжектируют мок-клиент в `OpenAIProvider`).
"""

from dataclasses import dataclass

from text_to_sql_agent.application.ports.llm_provider import LLMProvider
from text_to_sql_agent.application.ports.tracer import Tracer
from text_to_sql_agent.config.settings import Settings, load_settings
from text_to_sql_agent.infrastructure.llm.openai_provider import OpenAIProvider
from text_to_sql_agent.infrastructure.observability.jsonl_tracer import JsonlTracer


@dataclass(slots=True)
class Container:
    """Собранные зависимости приложения (наполняется по мере сессий)."""

    settings: Settings
    llm: LLMProvider
    tracer: Tracer


def build_container(settings: Settings | None = None) -> Container:
    """Дефолтная сборка; `settings=None` → fail-fast чтение окружения."""
    settings = settings or load_settings()
    tracer = JsonlTracer(settings.trace_jsonl_path)
    llm = OpenAIProvider(settings, tracer=tracer)
    return Container(settings=settings, llm=llm, tracer=tracer)
