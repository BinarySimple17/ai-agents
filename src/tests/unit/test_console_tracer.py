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


def test_observation_zero_rows_with_columns_renders_header_only() -> None:
    # Layer-3 S25: тул вернул 0 строк при непустых колонках (объединённый контур
    # feat-020: «SELECT DISTINCT …» пришёл пустым) — прежний max(len(name), *())
    # на пустых cells давал TypeError «'int' object is not iterable», рендер
    # падал (RuntimeWarning, событие терялось). Соседний тест с columns=[] не
    # ловил: ранний выход `if not columns` до расчёта ширин. Регресс: падает
    # на до-фиксовом коде
    tracer, stream = _tracer()
    tracer.record(
        Trace(
            event="agent.observation",
            payload={
                "ok": True,
                "columns": ["channel", "cnt"],
                "rows_preview": [],
                "row_count": 0,
                "truncated": False,
            },
        )
    )
    # Рендер: заголовок результата + строка имён колонок без данных (информативно
    # для оператора — он видит, какие колонки БЫЛИ у пустого результата)
    assert stream.getvalue() == "  ← 0 строк (усечён: нет)\n    │ channel │ cnt │\n"


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


# --- FSM-события (S27 Task 1): этапы прогона с содержанием payload ---
# До S27 FSM-события шли generic-веткой (имя без payload) — консольный трейс
# не показывал план/валидацию/вердикт, артефакт hw-plan §5 был неполон.


def test_fsm_node_line() -> None:
    tracer, stream = _tracer()
    tracer.record(Trace(event="graph.node", payload={"node": "plan_joins"}))
    assert stream.getvalue() == "  → узел plan_joins\n"


def test_fsm_clarify_verdict_and_interrupt() -> None:
    tracer, stream = _tracer()
    tracer.record(
        Trace(
            event="clarify.verdict",
            payload={"user_id": "u_logist", "outcome": "clarify", "assumptions": 1},
        )
    )
    tracer.record(
        Trace(
            event="clarify.interrupt",
            payload={
                "thread_id": "abc123def",
                "user_id": "u_logist",
                "question": "Какие кампании?",
            },
        )
    )
    lines = stream.getvalue().splitlines()
    assert lines[0] == "  · clarify: исход clarify, допущений 1"
    assert lines[1] == "  ⏸ interrupt — ждёт ответа: Какие кампании?"


def test_fsm_plan_and_generate_verdicts() -> None:
    tracer, stream = _tracer()
    tracer.record(
        Trace(
            event="plan_joins.verdict",
            payload={
                "user_id": "u_logist",
                "outcome": "plan",
                "graph_version": 221,
                "candidate_tables": ["orders", "customers"],
                "hit_edge_ids": [],
            },
        )
    )
    tracer.record(
        Trace(
            event="generate_sql.verdict",
            payload={
                "user_id": "u",
                "outcome": "sql",
                "llm_calls": 1,
                "escalated": True,
                "tables": 2,
            },
        )
    )
    lines = stream.getvalue().splitlines()
    assert lines[0] == "  · plan_joins: исход plan, gv 221, кандидаты: orders, customers"
    assert lines[1] == "  · generate_sql: исход sql, таблиц 2, вызовов 1, эскалация да"


def test_fsm_validate_diagnose_verdicts() -> None:
    tracer, stream = _tracer()
    tracer.record(
        Trace(
            event="validate_sql.verdict",
            payload={"user_id": "u", "outcome": "ok", "issues_count": 0, "enforced_limit": 1000},
        )
    )
    tracer.record(
        Trace(
            event="diagnose.verdict",
            payload={
                "user_id": "u",
                "error_class": "sql_syntax",
                "retry_futile": True,
                "llm_calls": 1,
            },
        )
    )
    lines = stream.getvalue().splitlines()
    assert lines[0] == "  · validate_sql: исход ok, замечаний 0, LIMIT 1000"
    assert lines[1] == "  · diagnose: класс sql_syntax, вызовов 1, ретрай бесперспективен"


def test_fsm_probe_interpret_degrade_verdicts() -> None:
    tracer, stream = _tracer()
    tracer.record(
        Trace(
            event="execute.probe",
            payload={
                "user_id": "u",
                "tables": ["orders"],
                "counts": {"orders": 0},
                "verdict": "empty",
            },
        )
    )
    tracer.record(
        Trace(
            event="interpret.verdict",
            payload={
                "outcome": "answer",
                "incomplete": True,
                "llm_calls": 2,
                "row_count": 1,
                "cost_rub": 0.0,
                "truncated": False,
                "approximate": False,
            },
        )
    )
    tracer.record(
        Trace(
            event="degrade.verdict",
            payload={"cause": "no_path", "has_variant": True, "used_llm": False, "llm_calls": 0},
        )
    )
    lines = stream.getvalue().splitlines()
    assert lines[0] == "  · probe COUNT: empty (orders)"
    assert lines[1] == "  · interpret: answer, строк 1, 0.00₽, НЕПОЛНЫЙ ответ"
    assert lines[2] == "  · degrade: причина no_path, формулировка шаблон, вариант предложен"


def test_fsm_unavailable_events_stay_generic() -> None:
    # Семейство *.unavailable остаётся generic — деталь сбоя смотрят в JSONL
    tracer, stream = _tracer()
    tracer.record(Trace(event="plan_joins.unavailable", payload={"user_id": "u", "detail": "gv=0"}))
    assert stream.getvalue() == "  · plan_joins.unavailable\n"
