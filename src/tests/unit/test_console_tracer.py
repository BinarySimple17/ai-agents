"""Юнит-тесты ConsoleTracer/TeeTracer (S21 Task 4): рендер этапов агента в stderr.

Проверки: точные строки рендера; таблица результата ≤5 строк (BL-004 — данные
строками, хоть обрезанными); обрезка ячеек ≤20 / SQL ≤120 / ошибок ≤120;
неизвестное событие — generic-строка; best-effort (сбой печати не рвёт вызов);
TeeTracer рассылает всем приёмникам и переживает сбой одного.
"""

import io

import pytest

from text_to_sql_agent.domain.entities import Trace
from text_to_sql_agent.infrastructure.observability.composite_tracer import TeeTracer
from text_to_sql_agent.infrastructure.observability.console_tracer import ConsoleTracer


def _tracer() -> tuple[ConsoleTracer, io.StringIO]:
    stream = io.StringIO()
    return ConsoleTracer(stream=stream), stream


def test_start_and_round_lines() -> None:
    tracer, stream = _tracer()
    tracer.record(Trace(event="agent.start", payload={"question": "Сколько?"}))
    tracer.record(Trace(event="agent.round", payload={"call": 1}))
    assert stream.getvalue() == "▶ Вопрос: Сколько?\n─ Ход 1\n"


def test_llm_complete_line_with_reasoning_mark() -> None:
    tracer, stream = _tracer()
    tracer.record(
        Trace(
            event="llm.complete",
            payload={
                "model": "qwen3.8-9b",
                "tokens_in": 584,
                "tokens_out": 212,
                "latency_ms": 4123,
                "cost_rub": 0.0,
                "text_from_reasoning": True,
            },
        )
    )
    assert stream.getvalue() == "  · qwen3.8-9b · 584→212 ток · 4.1с · 0.00₽ [ответ из reasoning]\n"


def test_llm_complete_line_with_stream_ttft() -> None:
    # S19 (NFR-1): стрим-вызов несёт ttft_ms — рендер видит время до первого чанка
    tracer, stream = _tracer()
    tracer.record(
        Trace(
            event="llm.complete",
            payload={
                "model": "qwen3.8-9b",
                "tokens_in": 584,
                "tokens_out": 212,
                "latency_ms": 4123,
                "cost_rub": 0.0,
                "stream": True,
                "ttft_ms": 430,
            },
        )
    )
    assert stream.getvalue() == "  · qwen3.8-9b · 584→212 ток · 4.1с · 0.00₽ · первый чанк 430мс\n"
    tracer.record(Trace(event="llm.complete", payload={"model": "m"}))  # буферный путь: поля нет
    assert "первый чанк" not in stream.getvalue().splitlines()[-1]


def test_tool_call_sql_truncated_to_one_line() -> None:
    tracer, stream = _tracer()
    tracer.record(Trace(event="agent.tool_call", payload={"name": "run_sql", "sql": "x" * 300}))
    line = stream.getvalue().rstrip("\n")
    assert line.startswith("  → run_sql: ")
    assert "\n" not in line  # SQL одной строкой
    assert line.endswith("…")
    sql_part = line.removeprefix("  → run_sql: ")
    assert len(sql_part) <= 120  # критерий Task 4: SQL ≤120 (точный ассерт, ревью S21)


def test_observation_table_max_five_rows() -> None:
    tracer, stream = _tracer()
    tracer.record(
        Trace(
            event="agent.observation",
            payload={
                "ok": True,
                "columns": ["customer_id", "name"],
                "rows_preview": [{"customer_id": n, "name": f"Клиент {n}"} for n in range(1, 9)],
                "row_count": 12,
                "truncated": False,
            },
        )
    )
    out = stream.getvalue()
    assert "  ← 12 строк (усечён: нет)\n" in out
    assert out.count("\n") == 8  # заголовок + шапка таблицы + 5 строк + «первые 5 из 12» + print
    assert "│ customer_id │ name" in out
    assert "│ 5           │ Клиент 5" in out
    assert "Клиент 6" not in out  # строк 6+ на консоли нет
    assert "… показаны первые 5 из 12" in out


def test_observation_cell_truncated() -> None:
    tracer, stream = _tracer()
    tracer.record(
        Trace(
            event="agent.observation",
            payload={
                "ok": True,
                "columns": ["name"],
                "rows_preview": [{"name": "И" * 50}],
                "row_count": 1,
                "truncated": False,
            },
        )
    )
    out = stream.getvalue()
    assert "…" in out
    row_line = out.splitlines()[2]  # первая строка данных таблицы
    cells = [part.strip() for part in row_line.split("│") if part.strip()]
    assert cells and all(len(cell) <= 20 for cell in cells)  # критерий Task 4: ячейка ≤20


def test_observation_error_line() -> None:
    tracer, stream = _tracer()
    tracer.record(
        Trace(
            event="agent.observation",
            payload={
                "ok": False,
                "error_class": "syntax",
                "error": f"БД отклонила запрос (syntax): {'x' * 200}",
            },
        )
    )
    line = stream.getvalue().rstrip("\n")
    assert line.startswith("  ← ОШИБКА syntax: ")
    assert "\n" not in line
    message_part = line.removeprefix("  ← ОШИБКА syntax: ")
    assert len(message_part) <= 120  # точный ассерт (ревью S21): текст ошибки ≤120


def test_observation_empty_result_only_header() -> None:
    tracer, stream = _tracer()
    tracer.record(
        Trace(
            event="agent.observation",
            payload={
                "ok": True,
                "columns": [],
                "rows_preview": [],
                "row_count": 0,
                "truncated": False,
            },
        )
    )
    assert stream.getvalue() == "  ← 0 строк (усечён: нет)\n"


def test_observation_columns_capped_not_silently() -> None:
    # Ревью S21 (Minor): кап колонок не молчит — оператор видит хвост
    tracer, stream = _tracer()
    tracer.record(
        Trace(
            event="agent.observation",
            payload={
                "ok": True,
                "columns": [f"col_{n}" for n in range(1, 13)],  # 12 колонок
                "rows_preview": [{f"col_{n}": n for n in range(1, 13)}],
                "row_count": 1,
                "truncated": False,
            },
        )
    )
    out = stream.getvalue()
    assert "col_8" in out
    assert "col_9" not in out.splitlines()[2]  # в таблице только первые 8
    assert "… ещё 4 колонки" in out  # хвост показан явно (согласование числительного)


def test_degrade_answer_and_unknown_event() -> None:
    tracer, stream = _tracer()
    tracer.record(Trace(event="agent.degrade", payload={"reason": "модель молчит"}))
    tracer.record(Trace(event="agent.answer", payload={"source": "text"}))
    tracer.record(Trace(event="agent.future_stage", payload={"x": 1}))
    assert "✗ деградация: модель молчит\n" in stream.getvalue()
    assert "✓ ответ (text)\n" in stream.getvalue()
    assert "  · agent.future_stage\n" in stream.getvalue()  # generic для будущих событий


def test_trouble_lines() -> None:
    tracer, stream = _tracer()
    tracer.record(
        Trace(event="llm.retry", payload={"role": "mid", "attempt": 2, "status_code": 429})
    )
    tracer.record(Trace(event="llm.timeout", payload={"role": "mid", "elapsed_ms": 60000}))
    tracer.record(Trace(event="llm.breaker_open", payload={"failures": 5}))
    tracer.record(Trace(event="llm.breaker_open", payload={"reason": "half-open probe failed"}))
    tracer.record(Trace(event="llm.budget_stop", payload={"budget_used_rub": 0.5, "cap_rub": 2.0}))
    assert "  ! шлюз: повтор (попытка 2, причина 429)" in stream.getvalue()
    assert "  ! шлюз: таймаут (mid, 60000мс)" in stream.getvalue()
    assert "  ! breaker открыт (сбоев подряд: 5)" in stream.getvalue()
    assert "  ! breaker открыт (half-open probe failed)" in stream.getvalue()
    assert "  ! бюджет исчерпан (0.50₽ из 2.00₽)" in stream.getvalue()


def test_console_best_effort_on_write_failure() -> None:
    # Политика best-effort: сбой печати — RuntimeWarning, не исключение из цикла

    class BrokenStream:
        def write(self, *_args: object) -> int:
            raise OSError("диск переполнен")

    tracer = ConsoleTracer(stream=BrokenStream())
    with pytest.warns(RuntimeWarning, match="ConsoleTracer"):
        tracer.record(Trace(event="agent.answer", payload={"source": "text"}))


def test_tee_fanout_and_resilience() -> None:
    stream_a, stream_b = io.StringIO(), io.StringIO()

    class FailingTracer:
        def record(self, trace: Trace) -> None:
            raise ValueError("приёмник сломан")

    tee = TeeTracer(ConsoleTracer(stream=stream_a), FailingTracer(), ConsoleTracer(stream=stream_b))
    with pytest.warns(RuntimeWarning, match="FailingTracer"):
        tee.record(Trace(event="agent.answer", payload={"source": "text"}))
    # событие дошло в ОБА живых приёмника, несмотря на сбой между ними
    assert "✓ ответ (text)" in stream_a.getvalue()
    assert "✓ ответ (text)" in stream_b.getvalue()


def test_llm_reject_stream_abort_renders_reason() -> None:
    # S19 (ревью-2 m5): обрыв стрима — llm.reject без HTTP-статуса, с reason
    tracer, stream = _tracer()
    tracer.record(
        Trace(
            event="llm.reject",
            payload={"role": "mid", "status_code": None, "reason": "stream_abort"},
        )
    )
    assert "  ! шлюз: отказ stream_abort (mid)" in stream.getvalue()
