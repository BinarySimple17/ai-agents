"""Юнит-тесты стриминга (feat-019, S19 Task 5) — шлюз ТОЛЬКО моками (S00 правило 7).

Два уровня одного контракта:
- адаптер `OpenAIProvider.complete_stream`: чанки собираются в полный ответ (a),
  TTFT в событии llm.complete (b, NFR-1), mid-stream сбой без ретрая —
  `_register_failure` + LLMRequestError (c), BudgetExceededError ДО вызова (d),
  reasoning-дельты не ответ по guard'у finish_reason (e, D-021-аналог),
  usage-паритет: шлюз без usage → usage_missing=true, стоимость 0 не маскируется
  (f, finding ARCH-GATE S19); бюджет/breaker — те же guard'ы, что у complete()
  (правило 4 плана: стрим — не обход экономики ADR-007);
- флоу retrieval (`run_retrieval_flow`): дельты → printer по мере генерации,
  хвост исхода — `_stream_outcome` (деградация поверх напечатанного, ретрай
  пустых ответов только ДО первой дельты — Ловушки-1).

Помощники мок-шлюза (_settings/_provider/_http_error) — переиспользование
двойников test_llm_client (тот же компонент), без дубля.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from apps.cli import DEGRADED_ANSWER, LLM_DEGRADED_TEMPLATE, run_retrieval_flow
from tests.unit.fakes import UNIT_CTX, RecordingTracer
from tests.unit.test_llm_client import _MESSAGES, _http_error, _provider, _settings
from text_to_sql_agent.application.ports.llm_provider import (
    BudgetExceededError,
    CircuitOpenError,
    LLMProviderError,
    LLMRequestError,
    StreamChunk,
    StreamFinal,
    Usage,
)
from text_to_sql_agent.infrastructure.observability.jsonl_tracer import JsonlTracer

_GATEWAY_URL = "https://gateway.test/api/v1/chat/completions"


# --- моки стрима шлюза: форма событий OpenAI-совместимого стрима ---
class _ScriptedStream:
    """Итерабельный «стрим шлюза»: элементы отдаёт, Exception бросает посреди;
    close() запоминается — адаптер обязан закрыть соединение (finally)."""

    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)
        self.closed = False

    def __iter__(self) -> "_ScriptedStream":
        return self

    def __next__(self) -> Any:
        if not self._events:
            raise StopIteration
        step = self._events.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def close(self) -> None:
        self.closed = True


def _chunk(
    content: str | None = None,
    reasoning: str | None = None,
    finish_reason: str | None = None,
) -> Any:
    """Обычный чанк стрима: delta с content/reasoning_content + (опц.) finish_reason."""
    return SimpleNamespace(
        model="cheap-1",
        usage=None,
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, reasoning_content=reasoning),
                finish_reason=finish_reason,
            )
        ],
    )


def _usage_chunk(prompt: int = 100, completion: int = 20) -> Any:
    """Терминальный чанк шлюза с usage и без выбора (форма include_usage)."""
    return SimpleNamespace(
        model="cheap-1",
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
        choices=[],
    )


def _sdk_chunk(delta: dict[str, Any], finish_reason: str | None = None) -> Any:
    """Настоящий SDK-объект чанка: extra-поле reasoning_content должно дожить до
    фолбэка через getattr (M3-прецедент test_llm_client — апгрейд SDK не должен
    молча отключить фолбэк, оставив тесты на SimpleNamespace зелёными)."""
    return openai.types.chat.ChatCompletionChunk.model_validate(
        {
            "id": "chunk-1",
            "object": "chat.completion.chunk",
            "created": 1795112403,
            "model": "cheap-1",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
    )


# --- (a) чанки собираются в полный ответ; StreamFinal — терминальный элемент ---
def test_stream_chunks_assemble_into_answer() -> None:
    stream = _ScriptedStream(
        [
            _chunk("Ответ: "),
            _chunk("42 заказа,"),
            _chunk(" из них 12 новых."),
            _chunk(finish_reason="stop"),
            _usage_chunk(),
        ]
    )
    provider, completions, _, _ = _provider([stream])
    items = list(provider.complete_stream("cheap", _MESSAGES))
    chunks = [item for item in items if isinstance(item, StreamChunk)]
    finals = [item for item in items if isinstance(item, StreamFinal)]
    assert "".join(c.text_delta for c in chunks) == "Ответ: 42 заказа, из них 12 новых."
    assert len(finals) == 1  # ровно один терминальный элемент, последним
    assert items[-1] is finals[0]
    assert finals[0].finish_reason == "stop"
    assert finals[0].model == "cheap-1"
    assert finals[0].usage == Usage(tokens_in=100, tokens_out=20)
    assert completions.kwargs[0]["stream"] is True  # SDK-вызов в стрим-режиме
    assert completions.kwargs[0]["temperature"] == 0.0  # ADR-008 — как у complete()


def test_stream_request_without_tools_omits_tools_param() -> None:
    provider, completions, _, _ = _provider([_ScriptedStream([_chunk("x")])])
    list(provider.complete_stream("cheap", _MESSAGES))
    assert "tools" not in completions.kwargs[0]


# --- (b) TTFT в событии llm.complete (NFR-1) ---
def test_stream_ttft_recorded_in_llm_complete(tmp_path: Path) -> None:
    tracer = JsonlTracer(tmp_path / "trace.jsonl")
    stream = _ScriptedStream([_chunk("часть 1"), _chunk(finish_reason="stop")])
    provider, _, _, _ = _provider([stream], tracer=tracer)
    list(provider.complete_stream("cheap", _MESSAGES))
    record = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["event"] == "llm.complete"
    assert record["payload"]["stream"] is True  # расширение существующего события (W2)
    assert record["payload"]["ttft_ms"] == 0  # инъектированные часы стоят — 0мс
    assert record["payload"]["finish_reason"] == "stop"


# --- (f) usage-паритет: шлюз без usage — стоимость 0 не маскируется ---
def test_stream_without_usage_flags_usage_missing(tmp_path: Path) -> None:
    tracer = JsonlTracer(tmp_path / "trace.jsonl")
    stream = _ScriptedStream([_chunk("ответ"), _chunk(finish_reason="stop")])  # usage-чанка нет
    provider, _, _, _ = _provider([stream], tracer=tracer)
    items = list(provider.complete_stream("cheap", _MESSAGES))
    final = next(item for item in items if isinstance(item, StreamFinal))
    assert final.usage == Usage()  # пустой usage в терминальном элементе
    assert provider.budget_used_rub == 0.0
    record = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["payload"]["usage_missing"] is True  # finding ARCH-GATE S19


def test_stream_usage_counts_toward_budget() -> None:
    # Правило 4 плана: стрим расходует бюджет как обычный вызов (не обход ADR-007)
    stream = _ScriptedStream([_chunk("ответ"), _chunk(finish_reason="stop"), _usage_chunk()])
    provider, _, _, _ = _provider([stream])
    list(provider.complete_stream("cheap", _MESSAGES))
    # 100 in × 3.2₽/1М + 20 out × 13₽/1М = 0.00058 ₽ — та же арифметика, что complete()
    assert provider.budget_used_rub == pytest.approx(0.00058, rel=1e-6)
    assert provider.consecutive_failures == 0  # успех сбрасывает счётчик сбоев


# --- (e) reasoning-дельты: буфер, фолбэк по guard'у finish_reason (D-021-аналог) ---
def test_stream_reasoning_not_yielded_when_content_present() -> None:
    # Content главнее think-поля (прецедент complete()); reasoning не доходит
    # до вызывающего вовсе — stdout его не напечатает
    stream = _ScriptedStream([_chunk(reasoning="черновик размышления"), _chunk("Финальный ответ")])
    provider, _, _, _ = _provider([stream])
    items = list(provider.complete_stream("cheap", _MESSAGES))
    assert [item.text_delta for item in items if isinstance(item, StreamChunk)] == [
        "Финальный ответ"
    ]
    final = next(item for item in items if isinstance(item, StreamFinal))
    assert final.text_from_reasoning is False


def test_stream_reasoning_fallback_when_content_empty() -> None:
    # Пустой content при живом reasoning: фолбэк ОДНИМ чанком (ruling W4) —
    # вызывающий печатает его как обычную дельту; provenance в StreamFinal и трейсе
    stream = _ScriptedStream(
        [
            _chunk(reasoning="Ответ"),
            _chunk(reasoning=" из reasoning."),
            _chunk(finish_reason="stop"),
        ]
    )
    provider, _, _, _ = _provider([stream])
    items = list(provider.complete_stream("cheap", _MESSAGES))
    assert [item.text_delta for item in items if isinstance(item, StreamChunk)] == [
        "Ответ из reasoning."
    ]
    final = next(item for item in items if isinstance(item, StreamFinal))
    assert final.text_from_reasoning is True


def test_stream_real_sdk_reasoning_survives_to_fallback() -> None:
    stream = _ScriptedStream(
        [
            _sdk_chunk({"role": "assistant", "content": ""}),
            _sdk_chunk({"content": "", "reasoning_content": "Ответ из reasoning."}, "stop"),
        ]
    )
    provider, _, _, _ = _provider([stream])
    items = list(provider.complete_stream("cheap", _MESSAGES))
    assert [item.text_delta for item in items if isinstance(item, StreamChunk)] == [
        "Ответ из reasoning."
    ]


def test_stream_reasoning_not_promoted_on_no_text_finish_reasons() -> None:
    # Guard по finish_reason (I1-прецедент S03): обрыв по лимиту, блокировка
    # фильтром, несогласованный tool-ход — reasoning ответом не становится
    for reason in ("length", "content_filter", "tool_calls", "function_call"):
        stream = _ScriptedStream(
            [_chunk(reasoning="оборванная мысль…"), _chunk(finish_reason=reason)]
        )
        provider, _, _, _ = _provider([stream])
        items = list(provider.complete_stream("cheap", _MESSAGES))
        assert [item for item in items if isinstance(item, StreamChunk)] == [], reason
        final = next(item for item in items if isinstance(item, StreamFinal))
        assert final.finish_reason == reason and final.text_from_reasoning is False, reason


# --- (c) mid-stream сбой: без ретрая, breaker считает, соединение закрыто ---
def test_stream_mid_stream_error_no_retry_counts_failure() -> None:
    stream = _ScriptedStream(
        [
            _chunk("частичный "),
            _chunk("ответ"),
            openai.APIConnectionError(message="boom", request=httpx.Request("POST", _GATEWAY_URL)),
        ]
    )
    provider, completions, sleeps, _ = _provider([stream])
    with pytest.raises(LLMRequestError, match="стрим оборван"):
        list(provider.complete_stream("cheap", _MESSAGES))
    assert completions.calls == 1  # ретрая нет — частичный ответ уже напечатан
    assert sleeps == []
    assert provider.consecutive_failures == 1  # обрыв стрима = сбой шлюза (W3)
    assert stream.closed  # соединение закрыто (finally)


def test_stream_success_closes_stream() -> None:
    stream = _ScriptedStream([_chunk("ответ"), _chunk(finish_reason="stop")])
    provider, _, _, _ = _provider([stream])
    list(provider.complete_stream("cheap", _MESSAGES))
    assert stream.closed


# --- (d) бюджет/breaker ДО вызова — те же guard'ы, что у complete() ---
def test_stream_budget_exceeded_before_call() -> None:
    # Кап 0.0001₽: первый стрим (с usage-чанком) переполняет копилку, второй —
    # отказ ДО шлюза (pre-колл-гвард complete() без изменений)
    settings = _settings(budget_per_request_rub=0.0001)
    provider, completions, _, _ = _provider(
        [
            _ScriptedStream([_chunk("ответ"), _chunk(finish_reason="stop"), _usage_chunk()]),
            _ScriptedStream([]),
        ],
        settings=settings,
    )
    list(provider.complete_stream("cheap", _MESSAGES))  # копилка переполнена
    with pytest.raises(BudgetExceededError, match="кап бюджета"):
        provider.complete_stream("cheap", _MESSAGES)
    assert completions.calls == 1  # второй стрим шлюз не дёрнул


def test_stream_breaker_open_rejected_before_call() -> None:
    provider, completions, _, clock = _provider([])
    provider._breaker_open_until = clock[0] + 60.0  # breaker открыт (инъекция состояния)
    with pytest.raises(CircuitOpenError, match="breaker"):
        provider.complete_stream("cheap", _MESSAGES)
    assert completions.calls == 0


def test_stream_unknown_role_rejected_before_http() -> None:
    provider, completions, _, _ = _provider([])
    with pytest.raises(LLMProviderError, match="роль"):
        provider.complete_stream("mega", _MESSAGES)  # type: ignore[arg-type]
    assert completions.calls == 0


# --- ретраи только ДО первого чанка (ruling W3) ---
def test_stream_backoff_retries_before_first_chunk() -> None:
    # 429 на SDK-create — обычный backoff-путь (ADR-007 §4.3); чанков ещё не было
    stream = _ScriptedStream([_chunk("ответ"), _chunk(finish_reason="stop")])
    provider, completions, sleeps, _ = _provider([_http_error(429), stream])
    items = list(provider.complete_stream("cheap", _MESSAGES))
    assert "".join(c.text_delta for c in items if isinstance(c, StreamChunk)) == "ответ"
    assert completions.calls == 2
    assert 1.0 <= sleeps[0] <= 1.5  # base 1с + jitter ≤ 50%


def test_stream_empty_stream_yields_final_without_chunks() -> None:
    # Шлюз закрыл стрим, не отдав ни одного чанка: вызывающий получает
    # терминальный StreamFinal — решение (ретрай/деградация) за флоу
    stream = _ScriptedStream([])
    provider, _, _, _ = _provider([stream])
    items = list(provider.complete_stream("cheap", _MESSAGES))
    finals = [item for item in items if isinstance(item, StreamFinal)]
    assert len(finals) == 1 and finals[0].finish_reason is None


# --- флоу retrieval: дельты → printer, хвост исхода (Ловушки-1) ---
class _ScriptedFlowLLM:
    """Двойник LLMProvider со скриптованным стримом (S00 правило 7); Exception в
    сценарии бросается при итерации (как в _ScriptedStream адаптера), script не
    расходуется — повторный вызов (empty-retry) даёт тот же сценарий."""

    def __init__(self, script: list[Any]) -> None:
        self._script = script
        self.calls = 0

    def embed(self, texts):
        return [[0.1] * 1024 for _ in texts]

    def complete_stream(self, role, messages, tools=None):
        self.calls += 1
        for item in self._script:
            if isinstance(item, Exception):
                raise item
            yield item


class _FakeStore:
    def search(self, query_embedding, *, ctx, top_k):
        return []


def test_flow_streams_chunks_to_printer_as_generated() -> None:
    llm = _ScriptedFlowLLM(
        [
            StreamChunk(text_delta="Ответ "),
            StreamChunk(text_delta="42 заказа"),
            StreamFinal(model="fake", finish_reason="stop"),
        ]
    )
    printed: list[str] = []
    tracer = RecordingTracer()
    answer = run_retrieval_flow(
        "Сколько заказов?",
        llm=llm,
        vector_store=_FakeStore(),
        ctx=UNIT_CTX,
        tracer=tracer,
        printer=printed.append,
    )
    assert answer == "Ответ 42 заказа"
    assert printed == ["Ответ ", "42 заказа", "\n"]  # дельты по мере генерации + \n
    answer_event = next(e for e in tracer.events if e.event == "agent.answer")
    assert answer_event.payload["source"] == "retrieval_stream"
    # Текст ответа в консоль-трейс не попадает (Ловушка-4: рендер факта, не текста)
    assert "42 заказа" not in answer_event.model_dump_json()


def test_flow_mid_stream_failure_degrades_over_printed_chunks() -> None:
    llm = _ScriptedFlowLLM(
        [StreamChunk(text_delta="частичный "), LLMRequestError("стрим оборван шлюзом")]
    )
    printed: list[str] = []
    tracer = RecordingTracer()
    answer = run_retrieval_flow(
        "вопрос",
        llm=llm,
        vector_store=_FakeStore(),
        ctx=UNIT_CTX,
        tracer=tracer,
        printer=printed.append,
    )
    message = LLM_DEGRADED_TEMPLATE.format(error="стрим оборван шлюзом")
    assert answer == "частичный \n" + message  # деградация поверх напечатанного
    assert printed == ["частичный ", "\n", message, "\n"]  # без дубля дельт
    degrade = next(e for e in tracer.events if e.event == "agent.degrade")
    assert "оборвал" in degrade.payload["reason"]


def test_flow_mid_stream_failure_before_first_chunk_degrades() -> None:
    llm = _ScriptedFlowLLM([LLMRequestError("шлюз не ответил после 4 попыток")])
    printed: list[str] = []
    answer = run_retrieval_flow(
        "вопрос", llm=llm, vector_store=_FakeStore(), ctx=UNIT_CTX, printer=printed.append
    )
    message = LLM_DEGRADED_TEMPLATE.format(error="шлюз не ответил после 4 попыток")
    assert answer == message  # валидный исход, не traceback (S00 правило 8)
    assert printed == [message, "\n"]  # дельт не было — только сообщение деградации


def test_flow_empty_stream_retries_only_before_printing() -> None:
    # Пустой стрим (ни одной дельты): empty-retry не расходует ничего напечатанного;
    # исчерпание — деградация. calls = 1 + EMPTY_RESPONSE_RETRIES
    llm = _ScriptedFlowLLM([StreamFinal(model="fake", finish_reason="stop")])
    printed: list[str] = []
    answer = run_retrieval_flow(
        "вопрос", llm=llm, vector_store=_FakeStore(), ctx=UNIT_CTX, printer=printed.append
    )
    assert answer == DEGRADED_ANSWER
    assert printed == [DEGRADED_ANSWER, "\n"]  # дельт не было — только деградационный текст
    assert llm.calls == 3


def test_flow_finish_reason_length_degrades_without_retry() -> None:
    # Guard по finish_reason (e на уровне флоу): обрыв по лимиту — ретраить
    # бессмысленно, тот же исход; printer=None — мусор в ответ не попадает
    llm = _ScriptedFlowLLM([StreamFinal(model="fake", finish_reason="length")])
    tracer = RecordingTracer()
    answer = run_retrieval_flow(
        "вопрос", llm=llm, vector_store=_FakeStore(), ctx=UNIT_CTX, tracer=tracer
    )
    assert answer == DEGRADED_ANSWER
    assert llm.calls == 1  # без ретрая (в отличие от пустого ответа со stop)
    degrade = next(e for e in tracer.events if e.event == "agent.degrade")
    assert "finish_reason=length" in degrade.payload["reason"]


def test_flow_malformed_tag_not_returned_as_answer_without_printer() -> None:
    # Hermes-тег в контенте — битый формат (S13): printer=None — мусор не «ответ»
    llm = _ScriptedFlowLLM([StreamChunk(text_delta="<tool_call>{...}</tool_call>")])
    answer = run_retrieval_flow("вопрос", llm=llm, vector_store=_FakeStore(), ctx=UNIT_CTX)
    assert answer == DEGRADED_ANSWER


def test_flow_reasoning_fallback_chunk_printed_as_answer() -> None:
    # Фолбэк-чанк адаптера (W4) флоу печатает как обычную дельту — пользователь
    # видит ответ; llmProviderError-классов нет, исход валидный
    llm = _ScriptedFlowLLM(
        [
            StreamChunk(text_delta="Ответ из reasoning"),
            StreamFinal(model="fake", text_from_reasoning=True),
        ]
    )
    printed: list[str] = []
    answer = run_retrieval_flow(
        "вопрос", llm=llm, vector_store=_FakeStore(), ctx=UNIT_CTX, printer=printed.append
    )
    assert answer == "Ответ из reasoning"
    assert printed == ["Ответ из reasoning", "\n"]
