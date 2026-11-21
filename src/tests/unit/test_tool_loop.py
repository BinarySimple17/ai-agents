"""Юнит-тесты function-calling цикла CLI (ДЗ-02): кап ходов, Observation, деградация.

LLM и executor — моки (S00 правило 7); скриптованный шлюз «громкий»: исчерпанный
сценарий — AssertionError, а не молчаливый повтор.
"""

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from apps.cli import (
    DATA_MARKING,
    DEGRADED_ANSWER,
    EMPTY_RESPONSE_RETRIES,
    MAX_TOOL_CALLS,
    OBSERVATION_MAX_ROWS,
    OBSERVATION_PREVIEW_ROWS,
    SYSTEM_PROMPT,
    main,
    run_tool_loop,
)
from tests.unit.fakes import UNIT_CTX, FakeExecutor, RecordingTracer
from text_to_sql_agent.application.ports.llm_provider import CircuitOpenError, LLMResponse, ToolCall
from text_to_sql_agent.application.ports.sql_executor import (
    QueryResult,
    SQLExecutionError,
    SQLFailureClass,
)
from text_to_sql_agent.application.services.run_sql_tool import RunSqlTool
from text_to_sql_agent.infrastructure.observability.jsonl_tracer import JsonlTracer


class FakeLLM:
    """Скриптованный шлюз: ответы по очереди; параметры вызовов записываются."""

    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def complete(self, role, messages, *, tools=None):  # сигнатура порта LLMProvider
        self.calls.append({"role": role, "messages": messages, "tools": tools})
        assert self.script, "мок-шлюз вызван, но сценарий исчерпан"
        return self.script.pop(0)


class CircuitOpenLLM:
    """Шлюз с открытым breaker: каждый вызов бросает CircuitOpenError."""

    def complete(self, role, messages, *, tools=None):
        raise CircuitOpenError("circuit breaker открыт после серии сбоев шлюза")


def _text(text: str) -> LLMResponse:
    return LLMResponse(text=text, model="fake-1")


def _tool_call(sql: str = "SELECT 1", call_id: str = "call-1") -> LLMResponse:
    arguments = json.dumps({"sql": sql})
    return LLMResponse(
        text=None,
        tool_calls=[ToolCall(id=call_id, name="run_sql", arguments=arguments)],
        model="fake-1",
    )


def _loop(llm: FakeLLM, executor: FakeExecutor | None = None, tracer: object | None = None) -> str:
    tool = RunSqlTool(executor or FakeExecutor())
    return run_tool_loop("вопрос", llm=llm, tool=tool, ctx=UNIT_CTX, tracer=tracer)  # type: ignore[arg-type]


def test_immediate_answer_without_tool() -> None:
    llm = FakeLLM([_text("Готово")])
    assert _loop(llm) == "Готово"
    assert llm.calls[0]["tools"] is not None  # тул предлагается сразу


def test_tool_call_then_final_answer() -> None:
    llm = FakeLLM([_tool_call("SELECT 40 + 2"), _text("Ответ: 42")])
    assert _loop(llm) == "Ответ: 42"
    assert len(llm.calls) == 2
    tool_message = llm.calls[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call-1"
    assert DATA_MARKING in tool_message["content"]


def test_execution_error_returns_to_model_as_observation() -> None:
    executor = FakeExecutor(
        error=SQLExecutionError("БД отклонила запрос (timeout)", SQLFailureClass.TIMEOUT)
    )
    llm = FakeLLM([_tool_call(), _text("Упало по таймауту, упростил")])
    assert _loop(llm, executor) == "Упало по таймауту, упростил"
    tool_message = llm.calls[1]["messages"][-1]
    assert "ОШИБКА" in tool_message["content"]
    assert "timeout" in tool_message["content"]


def test_invalid_json_arguments_reported_not_raised() -> None:
    bad_call = LLMResponse(
        text=None,
        tool_calls=[ToolCall(id="call-1", name="run_sql", arguments="не-json")],
        model="fake-1",
    )
    llm = FakeLLM([bad_call, _text("Исправился")])
    assert _loop(llm) == "Исправился"
    assert "ОШИБКА" in llm.calls[1]["messages"][-1]["content"]


def test_unknown_tool_argument_reported_not_raised() -> None:
    bad_call = LLMResponse(
        text=None,
        tool_calls=[
            ToolCall(id="call-1", name="run_sql", arguments=json.dumps({"query": "SELECT 1"}))
        ],
        model="fake-1",
    )
    llm = FakeLLM([bad_call, _text("Исправился")])
    assert _loop(llm) == "Исправился"
    assert "неизвестные аргументы" in llm.calls[1]["messages"][-1]["content"]


def test_cap_exhausted_final_call_without_tools() -> None:
    script = [_tool_call(call_id=f"call-{n}") for n in range(MAX_TOOL_CALLS)]
    script.append(_text("Финал"))
    llm = FakeLLM(script)
    assert _loop(llm) == "Финал"
    assert len(llm.calls) == MAX_TOOL_CALLS + 1
    assert llm.calls[-1]["tools"] is None  # исчерпание → финальный вызов без tools (Q5)


def test_cap_exhausted_final_call_with_tags_degrades() -> None:
    # Наблюдение S13: пост-кап финальный вызов тоже может вернуть теги (тулов нет,
    # а модель ещё хочет запрос) — деградация, не сырые теги пользователю
    script = [_tool_call(call_id=f"call-{n}") for n in range(MAX_TOOL_CALLS)]
    script.append(LLMResponse(text="...</think>\n<tool_call>...</tool_call>", model="fake-1"))
    llm = FakeLLM(script)
    assert _loop(llm) == DEGRADED_ANSWER


def test_empty_response_retried_then_degraded() -> None:
    # EMPTY_RESPONSE_RETRIES = 2: три пустых хода подряд → деградация
    empty = LLMResponse(text=None, model="fake-1")
    llm = FakeLLM([empty, empty, empty])
    assert _loop(llm) == DEGRADED_ANSWER
    assert len(llm.calls) == 1 + 2  # 1 ход + 2 ретрая (в пределах одного хода)


def test_empty_retries_do_not_consume_tool_cap() -> None:
    empty = LLMResponse(text=None, model="fake-1")
    llm = FakeLLM([empty, empty, _tool_call(), _text("Готово")])
    assert _loop(llm) == "Готово"
    assert llm.calls[0]["tools"] is not None
    assert len(llm.calls) == 4


def test_malformed_tool_call_text_retried_not_printed() -> None:
    # Наблюдение S13 (layer-3): reasoning-модель иногда печатает Hermes-теги текстом
    malformed = LLMResponse(
        text="<tool_call>\n<function=run_sql>...</function>\n</tool_call>", model="fake-1"
    )
    llm = FakeLLM([malformed, _text("Готово")])
    assert _loop(llm) == "Готово"  # битый формат не отдаётся пользователю как ответ


def test_malformed_tool_call_thrice_degraded() -> None:
    malformed = LLMResponse(text="<tool_call>\n<function=run_sql>...</tool_call>", model="fake-1")
    llm = FakeLLM([malformed, malformed, malformed])
    assert _loop(llm) == DEGRADED_ANSWER


def test_think_tail_before_tags_still_retried() -> None:
    # Ловушка 6: reasoning приходит в content с `</think>`-хвостом ПЕРЕД тегами
    malformed = LLMResponse(
        text="...порассуждал...</think>\n\n<tool_call>\n<function=run_sql>...</tool_call>",
        model="fake-1",
    )
    llm = FakeLLM([malformed, _text("Готово")])
    assert _loop(llm) == "Готово"


def test_empty_response_then_answer() -> None:
    llm = FakeLLM([LLMResponse(text=None, model="fake-1"), _text("Ок")])
    assert _loop(llm) == "Ок"


def test_observation_rows_capped() -> None:
    many_rows = QueryResult(columns=["n"], rows=[{"n": n} for n in range(1, 26)], row_count=25)
    executor = FakeExecutor(result=many_rows)
    llm = FakeLLM([_tool_call(), _text("Готово")])
    _loop(llm, executor)
    content = llm.calls[1]["messages"][-1]["content"]
    assert f'"shown_rows": {OBSERVATION_MAX_ROWS}' in content
    assert '"row_count": 25' in content


def test_non_json_types_stringified_in_observation() -> None:
    # Регрессия на ревью S13 Critical-2: psycopg отдаёт numeric → Decimal и даты →
    # date — json.dumps без default ронял Observation и весь CLI на обычных колонках
    typed_rows = QueryResult(
        columns=["total", "day"],
        rows=[{"total": Decimal("12.50"), "day": date(2026, 1, 15)}],
        row_count=1,
    )
    executor = FakeExecutor(result=typed_rows)
    llm = FakeLLM([_tool_call(), _text("Готово")])
    _loop(llm, executor)
    content = llm.calls[1]["messages"][-1]["content"]
    assert '"total": "12.50"' in content
    assert '"day": "2026-01-15"' in content


def test_unknown_tool_name_not_dispatched() -> None:
    # Ревью S13 Minor-2: галлюцинированное имя тулa не исполняется молча
    hostile = LLMResponse(
        text=None,
        tool_calls=[ToolCall(id="call-1", name="delete_all", arguments="{}")],
        model="fake-1",
    )
    executor = FakeExecutor()
    llm = FakeLLM([hostile, _text("Готово")])
    assert _loop(llm, executor) == "Готово"
    assert executor.requests == []  # чужой тул не дошёл до канала SQL
    assert "неизвестный тул" in llm.calls[1]["messages"][-1]["content"]


def test_llm_provider_error_degrades_not_traceback() -> None:
    # Регрессия на ревью S13 Important-2: breaker/бюджет/ретраи шлюза — валидный
    # деградация-исход (S00 правило 8), не исключение из CLI
    llm = CircuitOpenLLM()
    answer = run_tool_loop("вопрос", llm=llm, tool=RunSqlTool(FakeExecutor()), ctx=UNIT_CTX)
    assert "шлюз LLM недоступен" in answer
    assert "circuit breaker" in answer


# --- события agent.* через порт Tracer (S21 Task 4, BL-004/D-022) ---
def test_events_sequence_tool_call_flow() -> None:
    tracer = RecordingTracer()
    llm = FakeLLM([_tool_call("SELECT 40 + 2"), _text("Ответ: 42")])
    assert _loop(llm, tracer=tracer) == "Ответ: 42"
    assert tracer.names() == [
        "agent.start",
        "agent.round",
        "agent.tool_call",
        "agent.observation",
        "agent.round",
        "agent.answer",
    ]
    start = tracer.events[0].payload
    assert start["question"] == "вопрос"
    assert start["trace_id"] == UNIT_CTX.trace_id  # корреляция §3 observability
    tool_event = tracer.events[2].payload
    assert tool_event["name"] == "run_sql"
    assert tool_event["sql"] == "SELECT 40 + 2"
    obs = tracer.events[3].payload
    assert obs["ok"] is True
    assert obs["columns"] == ["id"]
    assert obs["rows_preview"] == [{"id": 1}]
    assert obs["row_count"] == 1
    assert obs["truncated"] is False
    assert tracer.events[-1].payload["source"] == "text"


def test_observation_event_preview_capped_at_five() -> None:
    # BL-004 (требование владельца): результат строками, но превью ≤5;
    # контекст модели не меняется (OBSERVATION_MAX_ROWS=20 остаётся)
    rows = [{"n": n} for n in range(1, 11)]
    executor = FakeExecutor(result=QueryResult(columns=["n"], rows=rows, row_count=10))
    tracer = RecordingTracer()
    llm = FakeLLM([_tool_call(), _text("Готово")])
    _loop(llm, executor, tracer)
    obs = tracer.events[3].payload
    assert len(obs["rows_preview"]) == OBSERVATION_PREVIEW_ROWS
    assert obs["row_count"] == 10


def test_observation_error_event_carries_class() -> None:
    executor = FakeExecutor(
        error=SQLExecutionError("БД отклонила запрос (timeout)", SQLFailureClass.TIMEOUT)
    )
    tracer = RecordingTracer()
    llm = FakeLLM([_tool_call(), _text("Готово")])
    _loop(llm, executor, tracer)
    obs = tracer.events[3].payload
    assert obs["ok"] is False
    assert obs["error_class"] == "timeout"
    assert "timeout" in obs["error"]


def test_events_empty_retry_and_degrade() -> None:
    empty = LLMResponse(text=None, model="fake-1")
    tracer = RecordingTracer()
    llm = FakeLLM([empty, empty, empty])
    assert _loop(llm, tracer=tracer) == DEGRADED_ANSWER
    assert tracer.names().count("agent.empty_retry") == EMPTY_RESPONSE_RETRIES
    assert tracer.names()[-1] == "agent.degrade"


def test_events_cap_exhausted_and_final_no_tools() -> None:
    tracer = RecordingTracer()
    script = [_tool_call(call_id=f"call-{n}") for n in range(MAX_TOOL_CALLS)]
    script.append(_text("Финал"))
    llm = FakeLLM(script)
    assert _loop(llm, tracer=tracer) == "Финал"
    names = tracer.names()
    assert names.count("agent.tool_call") == MAX_TOOL_CALLS
    assert "agent.cap_exhausted" in names
    assert tracer.events[-1].payload["source"] == "final_no_tools"


def test_events_gateway_degradation() -> None:
    tracer = RecordingTracer()
    answer = run_tool_loop(
        "вопрос",
        llm=CircuitOpenLLM(),
        tool=RunSqlTool(FakeExecutor()),
        ctx=UNIT_CTX,
        tracer=tracer,  # type: ignore[arg-type] — тестовый двойник порта
    )
    assert "шлюз LLM недоступен" in answer
    assert tracer.names()[-1] == "agent.degrade"
    assert "circuit breaker" in tracer.events[-1].payload["reason"]


def test_observation_event_with_native_types_survives_jsonl_tracer(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Регрессия S21 Critical-1 (ревью): psycopg отдаёт Decimal/date — payload
    # события нёс сырые rows_preview, JsonlTracer без default=str падал TypeError
    # (--quiet ронял CLI), с TeeTracer событие observation молча терялось из JSONL
    typed_rows = QueryResult(
        columns=["total", "day"],
        rows=[{"total": Decimal("12.50"), "day": date(2026, 1, 15)}],
        row_count=1,
    )
    executor = FakeExecutor(result=typed_rows)
    llm = FakeLLM([_tool_call(), _text("Готово")])
    trace_path = tmp_path / "trace.jsonl"
    run_tool_loop(
        "вопрос", llm=llm, tool=RunSqlTool(executor), ctx=UNIT_CTX, tracer=JsonlTracer(trace_path)
    )
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    observations = [record for record in records if record["event"] == "agent.observation"]
    assert len(observations) == 1  # событие дошло до файла, а не потерялось с warning'ом
    assert observations[0]["payload"]["rows_preview"] == [{"total": "12.50", "day": "2026-01-15"}]


def test_task6_rows_last_and_prompt_semantics() -> None:
    # Guard ревью S21 (Minor): откат фикса «row_count: 1» должен падать тестом —
    # значения в конце payload (recency для малой модели) + пояснение в SYSTEM_PROMPT
    executor = FakeExecutor(result=QueryResult(columns=["n"], rows=[{"n": 7}], row_count=1))
    llm = FakeLLM([_tool_call(), _text("Готово")])
    _loop(llm, executor)
    content = llm.calls[1]["messages"][-1]["content"]
    payload = json.loads(content.splitlines()[1])
    assert list(payload)[-1] == "rows"
    assert "row_count/shown_rows" in SYSTEM_PROMPT


def test_system_prompt_carries_dictionary_codes() -> None:
    # S24 находка #2: модель тул-контура угадывала литерал страны ('Russia')
    # при сиде 'RU' (миграция 0003) — уверенный «0 клиентов из РФ» при реальных 5.
    # Тул-контур без retrieval (V4), схема и справочники — в промпте; регресс
    # на караул от отката: коды обоих справочников + негативный литерал +
    # самопроверка нулей перед ответом
    assert "country = 'RU'/'KZ'/'DE'/'BY'" in SYSTEM_PROMPT
    assert "status = 'paid'/'shipped'/'cancelled'" in SYSTEM_PROMPT
    assert "«Россия»/«Russia»" in SYSTEM_PROMPT  # негативные примеры в промпте
    assert "SELECT DISTINCT" in SYSTEM_PROMPT


def test_main_quiet_flag_disables_console_trace(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Ревью S21 (Minor): проводка --quiet → build_container(console_trace=False)
    # держалась только живым прогоном; переворот флага при рефакторинге — тест
    captured: dict[str, bool] = {}

    def fake_build_container(settings=None, *, console_trace=False):  # type: ignore[no-untyped-def]
        captured["console_trace"] = console_trace
        return SimpleNamespace(
            llm=CircuitOpenLLM(), run_sql_tool=RunSqlTool(FakeExecutor()), tracer=RecordingTracer()
        )

    monkeypatch.setattr("apps.cli.build_container", fake_build_container)
    # Проводка через --plain (тул-контур ДЗ-02): дефолтная ветка CLI теперь
    # объединённый контур (feat-020, нужен vector_store в контейнере); --quiet —
    # общий флаг обеих веток, проводка console_trace проверяется на своей
    assert main(["--plain", "--quiet", "вопрос"]) == 0
    assert captured["console_trace"] is False
    assert main(["--plain", "вопрос"]) == 0
    assert captured["console_trace"] is True
