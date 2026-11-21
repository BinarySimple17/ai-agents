"""Порт `LLMProvider` — контракт стыка 2 «LLMProvider ↔ узлы» (S00; решение ADR-007).

Типы ответа — pydantic-DTO стыка и живут здесь, не в domain: domain не знает
LLM-концепций (S00 «Запрещено», самопроверка grep по `domain/`). pydantic в портах
допустим — базовая лексика проекта (ARCH-GATE S03, note Q2).

Ошибки стыка типизированы: оркестратор (S04+) матчит `CircuitOpenError` /
`BudgetExceededError` на терминал `degrade` (ADR-007 §4.3, M6 — деградацию решает
FSM, не провайдер). Бюджетный учёт — порт `BudgetGuard` (S15); до S15 кап-предохранитель
резидент в адаптере `infrastructure/llm/openai_provider.py` (решение ARCH-GATE S03).
"""

from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

Role = Literal["cheap", "mid", "strong"]
Embedding = list[float]
# OpenAI-формат сообщений/тулов: узлы строят dicts, адаптер передаёт как есть.
Messages = Sequence[Mapping[str, Any]]
ToolSpec = Mapping[str, Any]

# finish_reason, при которых reasoning_content не может считаться ответом (D-021):
# length — текст оборван по лимиту токенов (частичный CoT); content_filter —
# контент заблокирован политикой шлюза (фолбэк не должен обходить блокировку);
# tool_calls/function_call — ход заявляет tool-вызов: текста в нём нет в любом
# случае. При переданных тулах tool_calls-финал — НОРМАЛЬНЫЙ ход (тул исполняет
# консумер, S25 feat-020 — объединённый контур), а не фолбэк; при tools=None —
# несогласованный шлюз (тул не запрашивался): путь — ретрай/деградация.
# Различение — консумер-сайв (tools переданы И tool_calls непусты), сам guard
# не меняется: reasoning тул-хода ответом не становится в обоих случаях.
# Строго "stop" не требуется — часть шлюзов поле не отдаёт (ревью S03 Important-1).
# Константа живёт в порту с S19: guard общий для буферного complete() и стрима
# (ruling W4 ARCH-GATE S19 — единый источник, без дубля в адаптере).
NO_TEXT_FALLBACK_REASONS = frozenset({"length", "content_filter", "tool_calls", "function_call"})


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


class StreamChunk(BaseModel):
    """Дельта ответа стрима (S19, стык 2): печатается вызывающим по мере генерации.

    Конкатенация всех text_delta генератора = полный текст ответа (семантика
    LLMResponse.text буферного пути). Reasoning-дельты в потоке не появляются —
    адаптер буферизует их и отдаёт одним чанком-фолбэком по guard'у
    finish_reason (D-021-аналог, ruling W4 ARCH-GATE S19).
    """

    text_delta: str


class StreamFinal(BaseModel):
    """Терминальный элемент стрима: метаданные завершённого вызова (S19, стык 2).

    usage может быть пустым (Usage()) — не все шлюзы шлют usage в стриме
    (finding ARCH-GATE S19): вызывающий отличает по событию llm.complete
    (`usage_missing=true`), стоимость 0 не маскируется. yield'ится ровно один,
    последним элементом генератора. finish_reason=None (стрим закрылся без
    явного статуса и без сбоя) — возможная тихая усечённость ответа шлюзом:
    интерпретирует это вызывающий (ревью S19 M6; «incomplete»-политика — V4/feat-015).
    """

    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None
    model: str
    text_from_reasoning: bool = False  # чанки стрима пришли из think-поля (фолбэк)
    # Тул-вызовы хода, собранные адаптером из дельт стрима (S25 feat-020) —
    # зеркало LLMResponse.tool_calls буферного пути: склейка по index, id/name —
    # на первой дельте вызова, arguments конкатенируются. Пуст — ход без тулов.
    tool_calls: list[ToolCall] = Field(default_factory=list)


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

    def complete_stream(
        self,
        role: Role,
        messages: Messages,
        *,
        tools: Sequence[ToolSpec] | None = None,
    ) -> Iterator[StreamChunk | StreamFinal]:
        """Потоковый вызов шлюза той же ролью (S19, ДЗ-03): дельты по мере генерации.

        Контракт (ruling W2/W3 ARCH-GATE S19): генератор `StreamChunk`-дельт,
        терминальный элемент — ровно один `StreamFinal` (usage/finish_reason/
        model; tool_calls — S25: тул-дельты склеиваются адаптером к финалу).
        Гарантии `complete()` — те же: бюджет/breaker guard'ы ДО вызова,
        backoff-политика ДО первого чанка; после первого чанка ретраев нет —
        частично напечатанный ответ ретраем не чинится, сбой → `LLMRequestError`
        (деградацию решает вызывающий, S00 правило 8). Деградация провайдера —
        те же классы ошибок, новых категорий не вводится. Существующий
        `complete()` не меняется — тул-контур и узлы FSM остаются на нём.

        Отказ от генератора до терминала (break/close — ревью M7): HTTP-стрим
        закрывается (`finally`), но учёт бюджета/событие llm.complete не
        выполняются — вызов «не завершён» (консуматор сам виноват в потере
        метаданных; Ctrl+C пользователя — тот же путь).
        """
        ...

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        """Векторы текстов (индексация DDL/глоссария/few-shot; реализация — S06)."""
        ...
