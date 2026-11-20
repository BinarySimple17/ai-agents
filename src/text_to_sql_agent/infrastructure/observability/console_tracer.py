"""Консольный рендер событий наблюдаемости — этапы работы агента в stderr (BL-004, D-022).

Порт `Tracer` — единственный шов наблюдаемости: цикл агента (`apps.cli`) и
провайдер эмитят события (`agent.*`, `llm.*`), ConsoleTracer рендерит их
человеку. Никаких print-ов в бизнес-коде — смена «формата» (OTel, файл) —
другой адаптер того же порта. Этапы — только stderr: stdout остаётся чистым
для ответа (пайпинг `cli … | tail -1` отдаёт ровно ответ).

Данные БД в наблюдении (`rows_preview`) — вид оператора-человека: консоль
показывает до OBSERVATION_PREVIEW_ROWS строк результата; контекст модели это
не затрагивает (S6 защищает модель, а не экран). Лимит превью и имена событий —
константы порта `tracer` (единый источник с эмиттером — ревью S21 I1/I2);
лимиты дисплея — константы ниже. Рендер best-effort: сбой печати не рвёт запрос
(политика JsonlTracer).
"""

import sys
import warnings
from typing import Any

from text_to_sql_agent.application.ports.tracer import (
    EVENT_AGENT_ANSWER,
    EVENT_AGENT_CAP_EXHAUSTED,
    EVENT_AGENT_DEGRADE,
    EVENT_AGENT_EMPTY_RETRY,
    EVENT_AGENT_OBSERVATION,
    EVENT_AGENT_ROUND,
    EVENT_AGENT_START,
    EVENT_AGENT_TOOL_CALL,
    OBSERVATION_PREVIEW_ROWS,
)
from text_to_sql_agent.domain.entities import Trace

_CELL_MAX_CHARS = 20  # ячейка таблицы результата (и имя колонки) — компактность строки
_COLUMNS_MAX = 8  # колонок в таблице — защита от раздутой строки консоли
# 100, не 120: с префиксом «  → run_sql: » (13) влезает в строку терминала —
# 120 давал визуальный перенос на скриншоте владельца (CLI.png, ревью S21)
_SQL_DISPLAY_MAX_CHARS = 100
_MESSAGE_MAX_CHARS = 120  # текст ошибки / вопрос / причина — одна строка консоли
_MS_PER_S = 1000.0

# События-неполадки шлюза: однострочные «!»-строки с ключевыми полями payload.
_TROUBLE_EVENTS = {"llm.retry", "llm.timeout", "llm.reject", "llm.breaker_open", "llm.budget_stop"}


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _one_line(text: Any, limit: int) -> str:
    return _truncate(" ".join(str(text).split()), limit)


def _cell(value: Any) -> str:
    return _one_line(value, _CELL_MAX_CHARS)


def _render(trace: Trace) -> str:
    event, payload = trace.event, trace.payload
    if event == EVENT_AGENT_START:
        return f"▶ Вопрос: {_one_line(payload.get('question', ''), _MESSAGE_MAX_CHARS)}"
    if event == EVENT_AGENT_ROUND:
        return f"─ Ход {payload.get('call', '?')}"
    if event == "llm.complete":
        return _render_llm_complete(payload)
    if event == EVENT_AGENT_TOOL_CALL:
        sql = _one_line(payload.get("sql", ""), _SQL_DISPLAY_MAX_CHARS)
        return f"  → {payload.get('name', '?')}: {sql}"
    if event == EVENT_AGENT_OBSERVATION:
        return _render_observation(payload)
    if event == EVENT_AGENT_EMPTY_RETRY:
        return f"  ! пустой ответ — повтор (осталось {payload.get('left', '?')})"
    if event == EVENT_AGENT_CAP_EXHAUSTED:
        return "  … кап ходов — финальный вызов без тулов"
    if event == EVENT_AGENT_DEGRADE:
        return f"✗ деградация: {_one_line(payload.get('reason', ''), _MESSAGE_MAX_CHARS)}"
    if event == EVENT_AGENT_ANSWER:
        return f"✓ ответ ({payload.get('source', '')})"
    if event in _TROUBLE_EVENTS:
        return _render_trouble(event, payload)
    return f"  · {event}"  # неизвестное событие — generic (устойчивость к будущим agent.*)


def _render_llm_complete(payload: dict[str, Any]) -> str:
    tokens = f"{payload.get('tokens_in', 0)}→{payload.get('tokens_out', 0)} ток"
    latency = f"{payload.get('latency_ms', 0) / _MS_PER_S:.1f}с"
    cost = f"{payload.get('cost_rub', 0.0):.2f}₽"
    line = f"  · {payload.get('model', '?')} · {tokens} · {latency} · {cost}"
    ttft_ms = payload.get("ttft_ms")  # стрим-путь (S19, NFR-1): время до первого чанка
    if ttft_ms is not None:
        # Миллисекунды (как elapsed у llm.timeout): суб-секундный TTFT в секундах
        # с одной десятичной — «0.0с», порог NFR-1 (≤ 3s) нечитаем (ревью M4)
        line += f" · первый чанк {ttft_ms}мс"
    if payload.get("usage_missing"):
        line += " (шлюз без usage)"
    if payload.get("text_from_reasoning"):
        line += " [ответ из reasoning]"
    return line


def _render_observation(payload: dict[str, Any]) -> str:
    if not payload.get("ok", False):
        error_class = payload.get("error_class", "?")
        message = _one_line(payload.get("error", ""), _MESSAGE_MAX_CHARS)
        return f"  ← ОШИБКА {error_class}: {message}"
    truncated = "да" if payload.get("truncated") else "нет"
    header = f"  ← {payload.get('row_count', 0)} строк (усечён: {truncated})"
    all_columns = [str(column) for column in (payload.get("columns") or [])]
    columns = all_columns[:_COLUMNS_MAX]  # кап не молча: хвост отображается ниже (ревью S21)
    rows = list(payload.get("rows_preview") or [])[:OBSERVATION_PREVIEW_ROWS]
    if not columns:
        return header
    names = [_cell(column) for column in columns]
    cells = [[_cell(row.get(column)) for column in columns] for row in rows]
    # ширина = максимум по колонке; ячейки и имена уже ≤ _CELL_MAX_CHARS, капа не нужно
    widths = [max(len(name), *(len(row[i]) for row in cells)) for i, name in enumerate(names)]
    lines = [
        header,
        "    │ "
        + " │ ".join(f"{name:<{width}}" for name, width in zip(names, widths, strict=True))
        + " │",
    ]
    lines.extend(
        "    │ "
        + " │ ".join(f"{cell:<{width}}" for cell, width in zip(row, widths, strict=True))
        + " │"
        for row in cells
    )
    row_count = payload.get("row_count", 0)
    if row_count > len(rows):
        lines.append(f"    … показаны первые {len(rows)} из {row_count}")
    hidden_columns = len(all_columns) - len(columns)
    if hidden_columns > 0:
        lines.append(
            f"    … ещё {hidden_columns} "
            f"{_plural(hidden_columns, 'колонка', 'колонки', 'колонок')}"
            " (полный набор — в JSONL-трейсе)"
        )
    return "\n".join(lines)


def _plural(count: int, one: str, few: str, many: str) -> str:
    # русское согласование числительного (ревью S21 Minor): 1 колонка, 2-4 колонки,
    # 5+ колонок; 11-14 — всегда «колонок»
    if 11 <= count % 100 <= 14:
        return many
    if count % 10 == 1:
        return one
    if 2 <= count % 10 <= 4:
        return few
    return many


def _render_trouble(event: str, payload: dict[str, Any]) -> str:
    # .get-стиль (ревью S21 Minor): переименование поля у провайдера даёт «?»,
    # а не KeyError-потерю строки
    if event == "llm.retry":
        attempt = payload.get("attempt", "?")
        reason = payload.get("status_code", "?")
        return f"  ! шлюз: повтор (попытка {attempt}, причина {reason})"
    if event == "llm.timeout":
        return f"  ! шлюз: таймаут ({payload.get('role', '?')}, {payload.get('elapsed_ms', '?')}мс)"
    if event == "llm.reject":
        status = payload.get("status_code")
        reason = payload.get("reason")  # стрим-путь (S19): HTTP-статуса нет — причина
        detail = f"HTTP {status}" if status is not None else (reason or "?")
        return f"  ! шлюз: отказ {detail} ({payload.get('role', '?')})"
    if event == "llm.breaker_open":
        detail = payload.get("reason") or f"сбоев подряд: {payload.get('failures', '?')}"
        return f"  ! breaker открыт ({detail})"
    if event == "llm.budget_stop":
        used = float(payload.get("budget_used_rub", 0))
        cap = float(payload.get("cap_rub", 0))
        return f"  ! бюджет исчерпан ({used:.2f}₽ из {cap:.2f}₽)"
    return f"  · {event}"  # недостижимо из _render (контроль полноты словаря событий)


class ConsoleTracer:
    """Адаптер `Tracer`: человекочитаемые этапы агента в stderr (best-effort)."""

    def __init__(self, stream: Any = None) -> None:
        # Инъекция потока — для тестов; по умолчанию системный stderr, захваченный
        # на момент конструирования (адаптер не пере-разрешает его на каждый вызов).
        self._stream = stream if stream is not None else sys.stderr

    def record(self, trace: Trace) -> None:
        try:
            print(_render(trace), file=self._stream, flush=True)
        except Exception as exc:  # best-effort: консоль не рвёт запрос (политика S2.2)
            warnings.warn(
                f"ConsoleTracer: рендер не удался ({exc}) — событие потеряно",
                RuntimeWarning,
                stacklevel=2,
            )
