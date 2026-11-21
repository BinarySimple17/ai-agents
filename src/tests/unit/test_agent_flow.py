"""Юнит-тесты объединённого контура ДЗ-03 (feat-020, run_agent_flow) — шлюз/
хранилище/тул ТОЛЬКО моки (S00 правило 7).

Матрица (план S25 Task 3): happy path (стрим преамбулы → tool_calls → observation →
стрим ответа с числом), ход text+tool_calls (ruling 3 — преамбула остаётся,
agent.answer не эмитится), кап ходов → финал без tools (ruling S13 Q5, S25
правило 2), пустой ответ ДО первой дельты (empty-retry), сбой mid-stream
(деградация поверх напечатанного — W3), Hermes-тег (I2 — возврат зеркалит экран,
S25 правило 4), недоступность retrieval (деградация КОНТЕКСТА, не прогона),
finish_reason-гвард (D-021), семантика S19 (_stream_outcome — переехала из
test_streaming вместе с контуром), wiring main (--plain / дефолт, чистота
stdout — D-022). Наблюдаемость llm.* адаптера — test_streaming.py.
"""

import json
from types import SimpleNamespace
from typing import Any

import pytest

from apps import cli as cli_module
from apps.cli import (
    AGENT_SYSTEM_PROMPT,
    DEGRADED_ANSWER,
    EMPTY_RESPONSE_RETRIES,
    LLM_DEGRADED_TEMPLATE,
    MAX_TOOL_CALLS,
    RETRIEVAL_TOP_K,
    run_agent_flow,
)
from tests.unit.fakes import UNIT_CTX, RecordingTracer
from tests.unit.test_retrieval_flow import _doc
from text_to_sql_agent.application.ports.llm_provider import (
    LLMProviderError,
    LLMRequestError,
    StreamChunk,
    StreamFinal,
    ToolCall,
)
from text_to_sql_agent.application.ports.sql_executor import (
    QueryResult,
    SQLExecutionError,
    SQLFailureClass,
)
from text_to_sql_agent.application.ports.vector_store import VectorStoreError
from text_to_sql_agent.application.services.run_sql_tool import TOOL_NAME
from text_to_sql_agent.infrastructure.observability.console_tracer import ConsoleTracer

_QUERY_VECTOR = [0.1] * 1024


class FakeAgentLLM:
    """Двойник LLMProvider со скриптованными ходами стрима (S00 правило 7).

    scripts[i] — сценарий i-го хода: StreamChunk/StreamFinal или Exception
    (бросается при итерации, как в _ScriptedStream адаптера). Сценарий последнего
    хода не расходуется — повторный вызов (empty-retry) даёт его же; вызовы
    запоминаются с messages/tools для проверок истории.
    """

    def __init__(self, scripts: list[list[Any]], *, embed_error: Exception | None = None) -> None:
        self._scripts = scripts
        self._embed_error = embed_error
        self.calls: list[dict[str, Any]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._embed_error is not None:
            raise self._embed_error
        return [_QUERY_VECTOR for _ in texts]

    def complete_stream(self, role: str, messages: list[dict], tools: Any = None):
        self.calls.append({"messages": messages, "tools": tools})
        index = min(len(self.calls) - 1, len(self._scripts) - 1)
        for item in self._scripts[index]:
            if isinstance(item, Exception):
                raise item
            yield item


class FakeStore:
    """Двойник VectorStore: выдаёт документы или ошибку, запоминает вызовы поиска."""

    def __init__(self, docs: list[Any] | None = None, error: Exception | None = None) -> None:
        self._docs = docs or []
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def search(self, query_embedding: list[float], *, ctx: Any, top_k: int) -> list[Any]:
        self.calls.append({"ctx": ctx, "top_k": top_k, "vector": query_embedding})
        if self._error is not None:
            raise self._error
        return self._docs


class FakeTool:
    """Двойник RunSqlTool: тот же tool_spec-контур; run отдаёт результат или ошибку."""

    tool_spec = {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}},
        },
    }

    def __init__(
        self, *, result: QueryResult | None = None, error: Exception | None = None
    ) -> None:
        # Дефолт — тематический результат «5 клиентов из РФ» (сид, миграция 0003)
        self._result = result or QueryResult(columns=["count"], rows=[{"count": 5}], row_count=1)
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def run(self, arguments: dict[str, Any], *, ctx: Any) -> QueryResult:
        self.calls.append({"arguments": arguments, "ctx": ctx})
        if self._error is not None:
            raise self._error
        return self._result


def _tool_turn(sql: str = "SELECT count(*) FROM customers WHERE country = 'RU'") -> list[Any]:
    """Ход с тул-вызовом: tool_calls в финале, без текстовых дельт."""
    return [
        StreamFinal(
            model="fake",
            finish_reason="tool_calls",
            tool_calls=[ToolCall(id="call-1", name=TOOL_NAME, arguments=json.dumps({"sql": sql}))],
        )
    ]


def _text_turn(text: str = "5 клиентов") -> list[Any]:
    """Текстовый ход: дельты + терминальный финал со stop."""
    return [StreamChunk(text_delta=text), StreamFinal(model="fake", finish_reason="stop")]


def test_agent_flow_happy_path_streams_answer_with_real_number() -> None:
    docs = [_doc("[таблица: orders] DDL", "ddl:orders")]
    llm = FakeAgentLLM([_tool_turn(), _text_turn("Ответ: 5 клиентов")])
    store, tool, printed, tracer = FakeStore(docs), FakeTool(), [], RecordingTracer()
    answer = run_agent_flow(
        "Сколько клиентов из РФ пришло из рекламных кампаний всего?",
        llm=llm,
        tool=tool,
        vector_store=store,
        ctx=UNIT_CTX,
        tracer=tracer,
        printer=printed.append,
    )
    assert answer == "Ответ: 5 клиентов"
    assert printed == ["Ответ: 5 клиентов", "\n"]  # дельты по мере генерации + \n
    # Retrieval: вопрос → эмбеддер, вектор + ctx + top_k → поиск с фильтром прав
    assert store.calls[0]["ctx"] == UNIT_CTX
    assert store.calls[0]["top_k"] == RETRIEVAL_TOP_K
    assert store.calls[0]["vector"] == _QUERY_VECTOR
    # Тул исполнен с ctx прогона; observation — в истории сообщений перед вторым ходом
    assert tool.calls[0]["ctx"] == UNIT_CTX
    assert tool.calls[0]["arguments"]["sql"].startswith("SELECT count(*)")
    second = llm.calls[1]["messages"]
    assert any(m["role"] == "tool" for m in second)
    assert any(m["role"] == "assistant" and m.get("tool_calls") for m in second)
    # Системный промпт: база тул-контура (S13) + retrieval-блок; tools переданы ходом
    assert llm.calls[0]["tools"] == [tool.tool_spec]
    assert "[таблица: orders] DDL" in llm.calls[0]["messages"][0]["content"]
    assert "run_sql" in llm.calls[0]["messages"][0]["content"]
    # Этапы: start(mode=agent) → retrieve → tool_call/observation → answer(agent_stream)
    start = next(e for e in tracer.events if e.event == "agent.start")
    assert start.payload["mode"] == "agent"
    assert next(e for e in tracer.events if e.event == "agent.retrieve").payload["docs"] == 1
    assert (
        next(e for e in tracer.events if e.event == "agent.tool_call").payload["name"] == TOOL_NAME
    )
    obs = next(e for e in tracer.events if e.event == "agent.observation")
    assert obs.payload["ok"] is True and obs.payload["row_count"] == 1
    answer_event = next(e for e in tracer.events if e.event == "agent.answer")
    assert answer_event.payload["source"] == "agent_stream"
    # Текст ответа в консоль-трейс не попадает (Ловушка-4: рендер факта, не текста)
    assert "5 клиентов" not in answer_event.model_dump_json()


def test_agent_flow_text_and_tool_calls_in_one_turn_keeps_preamble() -> None:
    # Ruling 3 S25: текст-дельты + tool_calls в одном финале — легальный поток
    # OpenAI. Напечатанная преамбула остаётся, ход исполняется, agent.answer
    # не эмитится до реального текстового финала, ретрай не нужен
    llm = FakeAgentLLM(
        [
            [
                StreamChunk(text_delta="Считаю клиентов… "),
                StreamFinal(
                    model="fake",
                    finish_reason="tool_calls",
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            name=TOOL_NAME,
                            arguments=json.dumps({"sql": "SELECT count(*) FROM customers"}),
                        )
                    ],
                ),
            ],
            _text_turn("5 клиентов"),
        ]
    )
    printed: list[str] = []
    tracer = RecordingTracer()
    answer = run_agent_flow(
        "вопрос",
        llm=llm,
        tool=FakeTool(),
        vector_store=FakeStore(),
        ctx=UNIT_CTX,
        tracer=tracer,
        printer=printed.append,
    )
    assert answer == "5 клиентов"
    assert printed[0] == "Считаю клиентов… "  # преамбула напечатана и осталась
    assert printed[-2] == "5 клиентов"
    assert sum(e.event == "agent.answer" for e in tracer.events) == 1  # только финальный
    assert len(llm.calls) == 2  # ретрая не было — ход исполнен


def test_agent_flow_cap_exhausted_final_streams_without_tools() -> None:
    # S25 правило 2: кап ходов — зеркало MAX_TOOL_CALLS тул-контура (константа
    # переиспользуется); исчерпание → финальный СТРИМ-вызов БЕЗ tools
    llm = FakeAgentLLM([_tool_turn(), _text_turn("Из Observation: 5 клиентов")])
    tracer = RecordingTracer()
    answer = run_agent_flow(
        "вопрос",
        llm=llm,
        tool=FakeTool(),
        vector_store=FakeStore(),
        ctx=UNIT_CTX,
        max_tool_calls=1,
        tracer=tracer,
        printer=[].append,
    )
    assert answer == "Из Observation: 5 клиентов"
    cap_event = next(e for e in tracer.events if e.event == "agent.cap_exhausted")
    assert cap_event.payload["rounds"] == 1
    assert llm.calls[1]["tools"] is None  # финальный вызов без тулов
    assert (
        next(e for e in tracer.events if e.event == "agent.answer").payload["source"]
        == "final_no_tools"
    )


def test_agent_flow_cap_constant_mirrors_tool_loop() -> None:
    # Вторая константа капа не заводится (S25 правило 2) — значение то же,
    # что у тул-контура ДЗ-02 (ruling S13 Q5: per-node ≤3)
    assert MAX_TOOL_CALLS == 3


def test_agent_flow_empty_turn_retries_only_before_printing() -> None:
    # Пустой ход (ни дельт, ни тулов): empty-retry не расходует напечатанное;
    # исчерпание — деградация. calls = 1 + EMPTY_RESPONSE_RETRIES
    llm = FakeAgentLLM([[StreamFinal(model="fake", finish_reason="stop")]])
    printed: list[str] = []
    answer = run_agent_flow(
        "вопрос",
        llm=llm,
        tool=FakeTool(),
        vector_store=FakeStore(),
        ctx=UNIT_CTX,
        tracer=RecordingTracer(),
        printer=printed.append,
    )
    assert answer == DEGRADED_ANSWER
    assert printed == [DEGRADED_ANSWER, "\n"]
    assert len(llm.calls) == 1 + EMPTY_RESPONSE_RETRIES


def test_agent_flow_empty_then_valid_turn_retries() -> None:
    # Первый ход пуст (stop, без дельт/тулов) — ретрай; второй валиден
    llm = FakeAgentLLM(
        [[StreamFinal(model="fake", finish_reason="stop")], _text_turn("5 клиентов")]
    )
    answer = run_agent_flow(
        "вопрос", llm=llm, tool=FakeTool(), vector_store=FakeStore(), ctx=UNIT_CTX
    )
    assert answer == "5 клиентов"
    assert len(llm.calls) == 2


def test_agent_flow_mid_stream_failure_degrades_over_printed_chunks() -> None:
    # W3: mid-stream сбой — ретрай задублирует напечатанное; деградация поверх
    llm = FakeAgentLLM(
        [[StreamChunk(text_delta="частичный "), LLMRequestError("стрим оборван шлюзом")]]
    )
    printed: list[str] = []
    tracer = RecordingTracer()
    answer = run_agent_flow(
        "вопрос",
        llm=llm,
        tool=FakeTool(),
        vector_store=FakeStore(),
        ctx=UNIT_CTX,
        tracer=tracer,
        printer=printed.append,
    )
    message = LLM_DEGRADED_TEMPLATE.format(error="стрим оборван шлюзом")
    assert answer == "частичный \n" + message
    assert printed == ["частичный ", "\n", message, "\n"]  # без дубля дельт
    degrade = next(e for e in tracer.events if e.event == "agent.degrade")
    assert "оборвал" in degrade.payload["reason"]
    assert len(llm.calls) == 1  # ретрая нет


def test_agent_flow_failure_before_first_chunk_degrades() -> None:
    llm = FakeAgentLLM([[LLMRequestError("шлюз не ответил после 4 попыток")]])
    printed: list[str] = []
    answer = run_agent_flow(
        "вопрос",
        llm=llm,
        tool=FakeTool(),
        vector_store=FakeStore(),
        ctx=UNIT_CTX,
        tracer=RecordingTracer(),
        printer=printed.append,
    )
    message = LLM_DEGRADED_TEMPLATE.format(error="шлюз не ответил после 4 попыток")
    assert answer == message  # валидный исход, не traceback (S00 правило 8)
    assert printed == [message, "\n"]


def test_agent_flow_finish_reason_length_degrades_without_retry() -> None:
    # Guard по finish_reason (D-021): обрыв по лимиту — ретраить бессмысленно
    llm = FakeAgentLLM([[StreamFinal(model="fake", finish_reason="length")]])
    tracer = RecordingTracer()
    answer = run_agent_flow(
        "вопрос", llm=llm, tool=FakeTool(), vector_store=FakeStore(), ctx=UNIT_CTX, tracer=tracer
    )
    assert answer == DEGRADED_ANSWER
    assert len(llm.calls) == 1  # без ретрая (в отличие от пустого ответа со stop)
    degrade = next(e for e in tracer.events if e.event == "agent.degrade")
    assert "finish_reason=length" in degrade.payload["reason"]


def test_agent_flow_malformed_tag_not_returned_as_answer_without_printer() -> None:
    # Hermes-тег в контенте — битый формат (S13): printer=None — мусор не «ответ»
    llm = FakeAgentLLM([[StreamChunk(text_delta="<tool_call>{...}</tool_call>")]])
    answer = run_agent_flow(
        "вопрос", llm=llm, tool=FakeTool(), vector_store=FakeStore(), ctx=UNIT_CTX
    )
    assert answer == DEGRADED_ANSWER


def test_agent_flow_malformed_tag_with_printer_degrades_over_printed_garbage() -> None:
    # I2 (S25 правило 4 — риск с тула́ми выше, чем был в retrieval): тег мог
    # напечататься до распознания — стрим не отзывает напечатанное, возврат
    # зеркалит экран (S19-семантика)
    llm = FakeAgentLLM([[StreamChunk(text_delta="<tool_call>{...}")]])
    printed: list[str] = []
    tracer = RecordingTracer()
    answer = run_agent_flow(
        "вопрос",
        llm=llm,
        tool=FakeTool(),
        vector_store=FakeStore(),
        ctx=UNIT_CTX,
        tracer=tracer,
        printer=printed.append,
    )
    assert answer == "<tool_call>{...}\n" + DEGRADED_ANSWER  # мусор не «ответ» в исходе
    assert printed == ["<tool_call>{...}", "\n", DEGRADED_ANSWER, "\n"]  # но экран уже видел
    degrade = next(e for e in tracer.events if e.event == "agent.degrade")
    assert "Hermes-тег" in degrade.payload["reason"]


def test_agent_flow_reasoning_fallback_chunk_printed_as_answer() -> None:
    # Фолбэк-чанк адаптера (W4 S19) печатается как обычная дельта — пользователь
    # видит ответ; исход валидный
    llm = FakeAgentLLM(
        [
            [
                StreamChunk(text_delta="Ответ из reasoning"),
                StreamFinal(model="fake", finish_reason="stop", text_from_reasoning=True),
            ]
        ]
    )
    printed: list[str] = []
    answer = run_agent_flow(
        "вопрос",
        llm=llm,
        tool=FakeTool(),
        vector_store=FakeStore(),
        ctx=UNIT_CTX,
        tracer=RecordingTracer(),
        printer=printed.append,
    )
    assert answer == "Ответ из reasoning"
    assert printed == ["Ответ из reasoning", "\n"]


def test_agent_flow_embed_failure_degrades_context_not_run() -> None:
    # Недоступность retrieval — деградация КОНТЕКСТА, не прогона (S25: тул-база
    # в промпте S13 позволяет ответить и без контекста; факт — agent.degrade)
    llm = FakeAgentLLM(
        [_tool_turn(), _text_turn("5 клиентов")], embed_error=LLMProviderError("шлюз недоступен")
    )
    store, tracer = FakeStore(), RecordingTracer()
    answer = run_agent_flow(
        "вопрос", llm=llm, tool=FakeTool(), vector_store=store, ctx=UNIT_CTX, tracer=tracer
    )
    assert answer == "5 клиентов"  # контур продолжил: тул исполнен
    assert store.calls == []  # поиск не вызывался
    degrade = next(e for e in tracer.events if e.event == "agent.degrade")
    assert "retrieval недоступен" in degrade.payload["reason"]
    # Блока контекста нет — системный промпт РОВНО база тул-контура (S13)
    assert llm.calls[0]["messages"][0]["content"] == AGENT_SYSTEM_PROMPT
    assert not any(e.event == "agent.retrieve" for e in tracer.events)


def test_agent_flow_store_failure_degrades_context_not_run() -> None:
    store = FakeStore(error=VectorStoreError("векторная память недоступна"))
    llm = FakeAgentLLM([_tool_turn(), _text_turn("5 клиентов")])
    tracer = RecordingTracer()
    answer = run_agent_flow(
        "вопрос", llm=llm, tool=FakeTool(), vector_store=store, ctx=UNIT_CTX, tracer=tracer
    )
    assert answer == "5 клиентов"
    degrade = next(e for e in tracer.events if e.event == "agent.degrade")
    assert "retrieval недоступен" in degrade.payload["reason"]


def test_agent_flow_gateway_down_at_first_turn_degrades_valid_outcome() -> None:
    # Контекст добыт, но шлюз упал на первом ходе — деградация-ответ по правилу 8
    llm = FakeAgentLLM([[LLMProviderError("breaker открыт")]])
    tracer = RecordingTracer()
    answer = run_agent_flow(
        "вопрос",
        llm=llm,
        tool=FakeTool(),
        vector_store=FakeStore([_doc("[таблица: orders] DDL", "ddl:orders")]),
        ctx=UNIT_CTX,
        tracer=tracer,
    )
    assert answer == LLM_DEGRADED_TEMPLATE.format(error="breaker открыт")
    assert len(llm.calls) == 1  # retrieval прошёл, цикл деградировал на первом ходе
    assert len([e for e in tracer.events if e.event == "agent.degrade"]) == 1


def test_agent_flow_observation_error_is_data_not_crash() -> None:
    # Ошибка исполнения SQL — Observation-ошибка в истории (паритет S13), не
    # traceback: цикл продолжается, модель отвечает после ошибки
    tool = FakeTool(
        error=SQLExecutionError("БД отклонила запрос (timeout)", SQLFailureClass.TIMEOUT)
    )
    llm = FakeAgentLLM([_tool_turn(), _text_turn("Не удалось получить данные")])
    tracer = RecordingTracer()
    answer = run_agent_flow(
        "вопрос", llm=llm, tool=tool, vector_store=FakeStore(), ctx=UNIT_CTX, tracer=tracer
    )
    assert answer == "Не удалось получить данные"
    obs = next(e for e in tracer.events if e.event == "agent.observation")
    assert obs.payload["ok"] is False
    assert obs.payload["error_class"] == "timeout"
    second = llm.calls[1]["messages"]
    tool_message = next(m for m in second if m["role"] == "tool")
    assert "ОШИБКА" in tool_message["content"]  # модель видит ошибку как данные


def test_agent_flow_unknown_tool_name_not_dispatched_silently() -> None:
    # Галлюцинированное имя тулa не диспетчерируется молча (ревью S13 Minor-2)
    llm = FakeAgentLLM(
        [
            [
                StreamFinal(
                    model="fake",
                    finish_reason="tool_calls",
                    tool_calls=[
                        ToolCall(id="call-x", name="run_query", arguments='{"sql": "SELECT 1"}')
                    ],
                )
            ],
            _text_turn("Доступен только run_sql"),
        ]
    )
    tool, tracer = FakeTool(), RecordingTracer()
    answer = run_agent_flow(
        "вопрос", llm=llm, tool=tool, vector_store=FakeStore(), ctx=UNIT_CTX, tracer=tracer
    )
    assert answer == "Доступен только run_sql"
    assert tool.calls == []  # неизвестный тул не исполнялся
    obs = next(e for e in tracer.events if e.event == "agent.observation")
    assert obs.payload["ok"] is False and obs.payload["error_class"] == "unknown_tool"


# --- wiring main → флоу: --plain и дефолт, чистота stdout (D-022) ---
def _agent_container(tracer: Any, scripts: list[list[Any]] | None = None) -> SimpleNamespace:
    llm = FakeAgentLLM(scripts or [_text_turn("Ответ 42")])
    return SimpleNamespace(
        llm=llm, run_sql_tool=FakeTool(), vector_store=FakeStore(), tracer=tracer
    )


def test_main_default_streams_answer_no_double_print(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # Дефолт: стрим-флоу печатает дельты сам (включая финальный \n) — повторный
    # print в main задублировал бы ответ; здесь флоу замокан, stdout пуст
    container = _agent_container(RecordingTracer())
    agent_calls: list[dict] = []
    monkeypatch.setattr(
        cli_module, "run_agent_flow", lambda *args, **kw: agent_calls.append(kw) or "Ответ 42"
    )
    monkeypatch.setattr(cli_module, "build_container", lambda **kw: container)
    assert cli_module.main(["--quiet", "Сколько заказов?"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""  # без дубля print и этапов
    assert agent_calls[0]["printer"] is cli_module._print_stdout  # стрим в stdout
    assert agent_calls[0]["tool"] is container.run_sql_tool
    assert agent_calls[0]["vector_store"] is container.vector_store


def test_main_default_stages_go_to_stderr_not_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # Живой флоу (мок-LLM): ответ — РОВНО на stdout; этапы — в stderr (ConsoleTracer)
    container = _agent_container(ConsoleTracer())
    monkeypatch.setattr(cli_module, "build_container", lambda **kw: container)
    assert cli_module.main(["Сколько заказов?"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Ответ 42\n"
    assert "▶ Вопрос" in captured.err  # этапы — в stderr
    assert "Ответ 42" not in captured.err  # текст ответа не в трейсе (Ловушка-4)


def test_main_plain_runs_tool_loop_buffered(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    # --plain — артефакт ДЗ-02: прежний run_tool_loop (буферный complete(),
    # без retrieval/стрима), ответ печатается одним print'ом
    container = _agent_container(RecordingTracer())
    plain_calls: list[dict] = []
    monkeypatch.setattr(
        cli_module, "run_tool_loop", lambda *args, **kw: plain_calls.append(kw) or "Ответ 5"
    )
    agent_calls: list[dict] = []
    monkeypatch.setattr(
        cli_module, "run_agent_flow", lambda *args, **kw: agent_calls.append(kw) or "НЕ ДОЛЖНО"
    )
    monkeypatch.setattr(cli_module, "build_container", lambda **kw: container)
    assert cli_module.main(["--plain", "--quiet", "Сколько всего заказов?"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Ответ 5\n"  # буферный путь: print(answer)
    assert len(plain_calls) == 1 and agent_calls == []  # дефолтный флоу не вызывался
    assert plain_calls[0]["tool"] is container.run_sql_tool
