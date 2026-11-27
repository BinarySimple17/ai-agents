"""Юнит-тесты стриминга (feat-019, S19 Task 5; тул-дельты — S25 feat-020) — шлюз
ТОЛЬКО моками (S00 правило 7).

Уровень адаптера `OpenAIProvider.complete_stream`: чанки собираются в полный ответ
(a), TTFT в событии llm.complete (b, NFR-1), mid-stream сбой без ретрая —
`_register_failure` + LLMRequestError (c), BudgetExceededError ДО вызова (d),
reasoning-дельты не ответ по guard'у finish_reason (e, D-021-аналог), usage-паритет:
шлюз без usage → usage_missing=true, стоимость 0 не маскируется (f, finding
ARCH-GATE S19); тул-дельты склеиваются в StreamFinal.tool_calls (g, S25); бюджет/
breaker — те же guard'ы, что у complete() (правило 4 плана: стрим — не обход
экономики ADR-007). Флоу-семантика printer/`_stream_outcome` ретирена вместе с тул-контуром
(BL-048/D-041, S39); стриминговые тесты этого файла — только llm.* адаптера.

Помощники мок-шлюза (_settings/_provider/_http_error) — переиспользование
двойников test_llm_client (тот же компонент), без дубля.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from tests.unit.test_llm_client import (
    _MESSAGES,
    _http_error,
    _provider,
    _settings,
)
from text_to_sql_agent.application.ports.llm_provider import (
    BudgetExceededError,
    CircuitOpenError,
    LLMProviderError,
    LLMRequestError,
    StreamChunk,
    StreamFinal,
    ToolCall,
    Usage,
)
from text_to_sql_agent.infrastructure.llm.openai_provider import OpenAIProvider
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
    tool_calls: list[Any] | None = None,
) -> Any:
    """Обычный чанк стрима: delta с content/reasoning_content/tool_calls + (опц.) finish_reason."""
    return SimpleNamespace(
        model="cheap-1",
        usage=None,
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=content, reasoning_content=reasoning, tool_calls=tool_calls
                ),
                finish_reason=finish_reason,
            )
        ],
    )


def _tool_delta(
    index: int,
    *,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> Any:
    """Дельта тул-вызова (форма OpenAI-стрима): id/name — на первой дельте
    вызова, arguments дозревает конкатенацией по последующим; склейка по index."""
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments or ""),
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


# --- (g) тул-дельты стрима: накопление в StreamFinal.tool_calls (S25 feat-020) ---
def test_stream_tool_call_deltas_assemble_into_final() -> None:
    # Зеркало буферного пути: дельты тула склеиваются в полный ToolCall к
    # финалу (id/name на первой, arguments конкатенацией); в stdout тул-дельты
    # не печатаются — StreamChunk остаётся текстовой дельтой
    stream = _ScriptedStream(
        [
            _chunk(tool_calls=[_tool_delta(0, call_id="call-1", name="run_sql", arguments='{"sq')]),
            _chunk(tool_calls=[_tool_delta(0, arguments='l": "SELECT 1"}')]),
            _chunk(finish_reason="tool_calls"),
            _usage_chunk(),
        ]
    )
    provider, _, _, _ = _provider([stream])
    items = list(provider.complete_stream("cheap", _MESSAGES))
    assert [item for item in items if isinstance(item, StreamChunk)] == []  # stdout молчит
    final = next(item for item in items if isinstance(item, StreamFinal))
    assert final.finish_reason == "tool_calls"
    assert final.tool_calls == [
        ToolCall(id="call-1", name="run_sql", arguments='{"sql": "SELECT 1"}')
    ]


def test_stream_two_tool_calls_assembled_in_index_order() -> None:
    # Несколько тул-вызовов в одном ходе: склейка по index, порядок в списке —
    # по index (порядок запроса), интерливинг дельт не ломает сборку
    stream = _ScriptedStream(
        [
            _chunk(tool_calls=[_tool_delta(0, call_id="call-a", name="run_sql", arguments="{")]),
            _chunk(tool_calls=[_tool_delta(1, call_id="call-b", name="run_sql", arguments="{}")]),
            _chunk(tool_calls=[_tool_delta(0, arguments='"sql": "SELECT 1"}')]),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    provider, _, _, _ = _provider([stream])
    items = list(provider.complete_stream("cheap", _MESSAGES))
    final = next(item for item in items if isinstance(item, StreamFinal))
    assert [call.id for call in final.tool_calls] == ["call-a", "call-b"]
    assert final.tool_calls[0].arguments == '{"sql": "SELECT 1"}'
    assert final.tool_calls[1].arguments == "{}"


def test_stream_text_and_tool_calls_in_one_turn() -> None:
    # Ход «преамбула + тул-вызов» (ruling 3 S25): легальный поток OpenAI —
    # текст идёт дельтами (консумер печатает как преамбулу), тул-вызов собран
    # в финале; консумер сам решает исполнение (адаптер ничего не отбрасывает)
    sql_delta = _tool_delta(0, call_id="call-1", name="run_sql", arguments='{"sql": "SELECT 1"}')
    stream = _ScriptedStream(
        [
            _chunk("Считаю клиентов…"),
            _chunk(tool_calls=[sql_delta]),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    provider, _, _, _ = _provider([stream])
    items = list(provider.complete_stream("cheap", _MESSAGES))
    chunks = [item for item in items if isinstance(item, StreamChunk)]
    assert "".join(c.text_delta for c in chunks) == "Считаю клиентов…"
    final = next(item for item in items if isinstance(item, StreamFinal))
    assert final.tool_calls and final.tool_calls[0].name == "run_sql"


def test_stream_real_sdk_tool_call_delta_survives_to_final() -> None:
    # M3-прецедент reasoning-фолбэка, повторённый для тул-дельт: настоящий
    # SDK-чанк — апгрейд SDK не должен молча отключить накопление
    stream = _ScriptedStream(
        [
            _sdk_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-sdk",
                            "type": "function",
                            "function": {"name": "run_sql", "arguments": '{"sql": "SELECT'},
                        }
                    ]
                }
            ),
            _sdk_chunk(
                {
                    "tool_calls": [
                        {"index": 0, "type": "function", "function": {"arguments": ' 1"}'}}
                    ]
                },
                "tool_calls",
            ),
        ]
    )
    provider, _, _, _ = _provider([stream])
    items = list(provider.complete_stream("cheap", _MESSAGES))
    final = next(item for item in items if isinstance(item, StreamFinal))
    assert final.tool_calls == [
        ToolCall(id="call-sdk", name="run_sql", arguments='{"sql": "SELECT 1"}')
    ]


def test_stream_no_tool_deltas_keeps_empty_tool_calls() -> None:
    # Текстовый ход без тулов: поле не копит мусора — пустой список по умолчанию
    stream = _ScriptedStream([_chunk("Ответ"), _chunk(finish_reason="stop"), _usage_chunk()])
    provider, _, _, _ = _provider([stream])
    items = list(provider.complete_stream("cheap", _MESSAGES))
    final = next(item for item in items if isinstance(item, StreamFinal))
    assert final.tool_calls == []


def test_stream_tool_call_delta_without_id_gets_synthesized() -> None:
    # M7 (ревью S25): шлюз без id в дельтах — синтез call-stream-N (реальный id
    # перезаписывает при появлении); пустой id в истории строгие OpenAI-
    # совместимые шлюзы отбивают следующим ходом (HTTP 400)
    stream = _ScriptedStream(
        [
            _chunk(tool_calls=[_tool_delta(0, name="run_sql", arguments='{"sql": "SELECT 1"}')]),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    provider, _, _, _ = _provider([stream])
    items = list(provider.complete_stream("cheap", _MESSAGES))
    final = next(item for item in items if isinstance(item, StreamFinal))
    assert final.tool_calls[0].id == "call-stream-0"


def test_stream_reasoning_not_yielded_on_tool_turn_without_finish_reason() -> None:
    # M8 (ревью S25): тул-ход без finish_reason — reasoning преамбулой не идёт
    # (обещание StreamChunk: «reasoning-дельты в потоке не появляются»); guard-
    # константа NO_TEXT_FALLBACK_REASONS не тронута — расширение условия на месте
    stream = _ScriptedStream(
        [
            _chunk(reasoning="черновик размышления"),
            _chunk(tool_calls=[_tool_delta(0, call_id="call-1", name="run_sql", arguments="{}")]),
        ]  # finish_reason=None — шлюз не отдал
    )
    provider, _, _, _ = _provider([stream])
    items = list(provider.complete_stream("cheap", _MESSAGES))
    assert [item for item in items if isinstance(item, StreamChunk)] == []  # stdout молчит
    final = next(item for item in items if isinstance(item, StreamFinal))
    assert final.tool_calls and final.tool_calls[0].name == "run_sql"


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


# --- регресс C1 (ревью S19): обрыв ИТЕРАЦИИ стрима — сырые httpx-исключения ---
_SSE_CHUNK = json.dumps(
    {
        "id": "chunk-1",
        "object": "chat.completion.chunk",
        "created": 1795112403,
        "model": "cheap-1",
        "choices": [{"index": 0, "delta": {"content": "часть "}, "finish_reason": None}],
    }
)


class _BreakingTransport(httpx.BaseTransport):
    """Транспорт: 200 + SSE-поток из одного чанка, затем транспортная ошибка —
    форма реального обрыва соединения посреди стрима. openai оборачивает в
    APIError только SSE-error payload и ошибки первоначального send; ошибки
    итерации (ReadError/ReadTimeout) доходят до консуматора сырыми (проверено
    на openai 2.38.0 — ревью C1), SimpleNamespace-двойники этот путь не видят."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        def body() -> Iterator[bytes]:
            yield f"data: {_SSE_CHUNK}\n\n".encode()
            raise self._error

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())


def _real_stream_provider(error: Exception, tracer: JsonlTracer | None = None) -> OpenAIProvider:
    """Провайдер на НАСТОЯЩЕМ SDK-клиенте с рвущимся транспортом (S00 правило 7
    не нарушено: сети нет — httpx.MockTransport-подобный транспорт в памяти)."""
    client = openai.OpenAI(
        base_url="https://gateway.test/api/v1",
        api_key="sk-test",
        max_retries=0,
        http_client=httpx.Client(transport=_BreakingTransport(error)),
    )
    return OpenAIProvider(_settings(), client=client, tracer=tracer)


def test_stream_iteration_read_error_maps_to_request_error() -> None:
    # До фикса: httpx.ReadError не ловился (ловили только openai.APIError) —
    # traceback шёл в CLI, breaker не считал обрывы (C1)
    provider = _real_stream_provider(httpx.ReadError("connection reset"))
    with pytest.raises(LLMRequestError, match="стрим оборван"):
        list(provider.complete_stream("cheap", _MESSAGES))
    assert provider.consecutive_failures == 1  # обрыв = сбой шлюза (W3)


def test_stream_iteration_read_timeout_not_a_breaker_failure() -> None:
    # W2-паритет посреди стрима: модель молчит между чанками дольше таймаута —
    # «медлительность, не ошибка»: llm.timeout, счётчик сбоев не растёт
    provider = _real_stream_provider(httpx.ReadTimeout("read timed out"))
    with pytest.raises(LLMRequestError, match="посреди стрима"):
        list(provider.complete_stream("cheap", _MESSAGES))
    assert provider.consecutive_failures == 0


def test_stream_mid_stream_error_traced_with_stream_flag(tmp_path: Path) -> None:
    # Ревью M5: обрыв стрима виден в трейсе (существующее имя llm.reject +
    # поле stream — новых имён словарь §2.4 не получает, W2)
    tracer = JsonlTracer(tmp_path / "trace.jsonl")
    provider = _real_stream_provider(httpx.ReadError("connection reset"), tracer=tracer)
    with pytest.raises(LLMRequestError):
        list(provider.complete_stream("cheap", _MESSAGES))
    record = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["event"] == "llm.reject"
    assert record["payload"]["stream"] is True


# --- ревью I1: usage запрошен у шлюза, иначе кап бюджета в стриме инертен ---
def test_stream_requests_usage_from_gateway() -> None:
    provider, completions, _, _ = _provider([_ScriptedStream([_chunk("x")])])
    list(provider.complete_stream("cheap", _MESSAGES))
    assert completions.kwargs[0]["stream_options"] == {"include_usage": True}


# --- ревью M6/M7: отказ от генератора — соединение закрыто, метаданных нет ---
def test_stream_generator_abandoned_closes_stream_without_metadata() -> None:
    stream = _ScriptedStream([_chunk("часть 1"), _chunk("часть 2")])
    provider, _, _, _ = _provider([stream])
    gen = provider.complete_stream("cheap", _MESSAGES)
    next(gen)  # один чанк — и консуматор передумал
    gen.close()  # GeneratorExit внутрь генератора (finally закрывает HTTP-стрим)
    assert stream.closed
    assert provider.budget_used_rub == 0.0  # вызов «не завершён» — учёта нет (M7)


# --- ревью-2 m1: malformed SSE-кадр (прокси вставил HTML в SSE-поток) ---
class _GarbageFrameTransport(httpx.BaseTransport):
    """Транспорт: валидный чанк, затем SSE-кадр с не-JSON payload — путь
    json.JSONDecodeError из sse.json(), который SDK НЕ оборачивает."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        def body() -> Iterator[bytes]:
            yield f"data: {_SSE_CHUNK}\n\n".encode()
            yield b"data: {oops\n\n"

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())


def test_stream_malformed_sse_frame_maps_to_request_error(tmp_path: Path) -> None:
    # До фикса: json.JSONDecodeError не ловился (не httpx.HTTPError и не APIError)
    # — traceback в CLI, breaker молчал (ревью-2 m1, остаточный класс C1)
    tracer = JsonlTracer(tmp_path / "trace.jsonl")
    client = openai.OpenAI(
        base_url="https://gateway.test/api/v1",
        api_key="sk-test",
        max_retries=0,
        http_client=httpx.Client(transport=_GarbageFrameTransport()),
    )
    provider = OpenAIProvider(_settings(), client=client, tracer=tracer)
    with pytest.raises(LLMRequestError, match="стрим оборван"):
        list(provider.complete_stream("cheap", _MESSAGES))
    assert provider.consecutive_failures == 1  # обрыв = сбой шлюза
    record = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["event"] == "llm.reject"
    assert record["payload"]["reason"] == "stream_abort"
    assert record["payload"]["status_code"] is None  # не строковый sentinel (ревью-2 m5)


# --- ревью-2 m2: «сырой httpx»-сценарий без зависимости от версии SDK ---
def test_stream_raw_httpx_error_maps_to_request_error() -> None:
    # Real-SDK тест (обрыв соединения) в залоченном env приходит ОБЁРНУТЫМ
    # (APIConnectionError — его ловил и до-фиксовый except openai.APIError),
    # поэтому guard «сырых» транспортных ошибок — на скриптованном стриме:
    # httpx.ReadError попадает в консуматор без обёртки на любой версии SDK,
    # до фикса (except только openai.APIError) тест падал
    stream = _ScriptedStream([_chunk("часть "), httpx.ReadError("connection reset")])
    provider, completions, sleeps, _ = _provider([stream])
    with pytest.raises(LLMRequestError, match="стрим оборван"):
        list(provider.complete_stream("cheap", _MESSAGES))
    assert completions.calls == 1  # без ретрая (частичный текст уже напечатан)
    assert sleeps == []
    assert provider.consecutive_failures == 1
