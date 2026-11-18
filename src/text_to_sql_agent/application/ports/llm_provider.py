"""Порт `LLMProvider` — контракт стыка 2 «LLMProvider ↔ узлы» (S00; решение ADR-007).

Типы ответа — pydantic-DTO стыка и живут здесь, не в domain: domain не знает
LLM-концепций (S00 «Запрещено», самопроверка grep по `domain/`). pydantic в портах
допустим — базовая лексика проекта (ARCH-GATE S03, note Q2).

Ошибки стыка типизированы: оркестратор (S04+) матчит `CircuitOpenError` /
`BudgetExceededError` на терминал `degrade` (ADR-007 §4.3, M6 — деградацию решает
FSM, не провайдер). Бюджетный учёт — порт `BudgetGuard` (S15); до S15 кап-предохранитель
резидент в адаптере `infrastructure/llm/openai_provider.py` (решение ARCH-GATE S03).
"""

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

Role = Literal["cheap", "mid", "strong"]
Embedding = list[float]
# OpenAI-формат сообщений/тулов: узлы строят dicts, адаптер передаёт как есть.
Messages = Sequence[Mapping[str, Any]]
ToolSpec = Mapping[str, Any]


class ToolCall(BaseModel):
    """Вызов тула, запрошенный моделью (ADR-008: тулы передаются параметром)."""

    id: str
    name: str
    arguments: str  # JSON-объект аргументов в строковой форме шлюза


class Usage(BaseModel):
    """Расход токенов вызова (FIX-005) — из него считаются ₽ и node_timings."""

    tokens_in: int = 0
    tokens_out: int = 0


class LLMResponse(BaseModel):
    """Ответ шлюза: текст и/или tool_calls + usage + фактическая модель."""

    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str  # фактическое имя модели — роль → модель резолвит адаптер из Settings


class LLMProviderError(Exception):
    """Базовая ошибка стыка LLM (после исчерпания политики провайдера)."""


class LLMRequestError(LLMProviderError):
    """Шлюз не ответил: ретраи исчерпаны, таймаут или неповторяемый 4xx."""


class CircuitOpenError(LLMProviderError):
    """Circuit breaker открыт — вызов отклонён без HTTP (сбой подряд ≥ порога)."""


class BudgetExceededError(LLMProviderError):
    """Кап бюджета запроса исчерпан — исключение ДО вызова шлюза (S00 стык 2)."""


@runtime_checkable
class LLMProvider(Protocol):
    """Единый шлюз по роли (NFR-7): смена модели — параметр вызова, не правка узлов."""

    def complete(
        self,
        role: Role,
        messages: Messages,
        *,
        tools: Sequence[ToolSpec] | None = None,
    ) -> LLMResponse:
        """Один вызов шлюза ролью из политики ретраев M1 (попытка 1 — роль узла)."""
        ...

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        """Векторы текстов (индексация DDL/глоссария/few-shot; реализация — S06)."""
        ...
