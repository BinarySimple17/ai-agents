"""Консольный рендер событий наблюдаемости — этапы работы агента в stderr (BL-004, D-022).

Порт `Tracer` — единственный шов наблюдаемости: цикл агента (`apps.cli`) и
провайдер эмитят события (`agent.*`, `llm.*`), ConsoleTracer рендерит их
человеку. Никаких print-ов в бизнес-коде — смена «формата» (OTel, файл) —
другой адаптер того же порта. Этапы — только stderr: stdout остаётся чистым
для ответа (пайпинг `cli … | tail -1` отдаёт ровно ответ).

Данные БД в наблюдении (`rows_preview`) — вид оператора-человека: консоль
показывает до _PREVIEW_ROWS_MAX строк результата; контекст модели это не
затрагивает (S6 защищает модель, а не экран). Все лимиты дисплея — константы
ниже. Рендер best-effort: сбой печати не рвёт запрос (политика JsonlTracer).
"""

import sys
import warnings
from typing import Any

from text_to_sql_agent.domain.entities import Trace

_CELL_MAX_CHARS = 20  # ячейка таблицы результата — компактность строки консоли
_COLUMN_WIDTH_MAX = 24  # ширина колонки — выравнивание, не простыня
_SQL_DISPLAY_MAX_CHARS = 120  # SQL тулa — одна строка консоли
_MESSAGE_MAX_CHARS = 120  # текст ошибки — одна строка консоли
_PREVIEW_ROWS_MAX = 5  # требование владельца: данные строками, хоть обрезанными
_MS_PER_S = 1000.0

# События-неполадки шлюза: однострочные «!»-строки с ключевыми полями payload.
_TROUBLE_TEMPLATES: dict[str, str] = {
    "llm.retry": "  ! шлюз: повтор (попытка {attempt}, причина {status_code})",
    "llm.timeout": "  ! шлюз: таймаут ({role}, {elapsed_ms}мс)",
    "llm.reject": "  ! шлюз: отказ HTTP {status_code} ({role})",
    "llm.breaker_open": "  ! breaker открыт ({detail})",
    "llm.budget_stop": "  ! бюджет исчерпан ({budget_used_rub}₽ из {cap_rub}₽)",
}


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _cell(value: Any) -> str:
    text = " ".join(str(value).split())
    return _truncate(text, _CELL_MAX_CHARS)


def _render(trace: Trace) -> str:
    event, payload = trace.event, trace.payload
    if event == "agent.start":
        return f"▶ Вопрос: {payload.get('question', '')}"
    if event == "agent.round":
        return f"─ Ход {payload.get('call', '?')}"
    if event == "llm.complete":
        return _render_llm_complete(payload)
    if event == "agent.tool_call":
        sql = _truncate(" ".join(str(payload.get("sql", "")).split()), _SQL_DISPLAY_MAX_CHARS)
        return f"  → {payload.get('name', '?')}: {sql}"
    if event == "agent.observation":
        return _render_observation(payload)
    if event == "agent.empty_retry":
        return f"  ! пустой ответ — повтор (осталось {payload.get('left', '?')})"
    if event == "agent.cap_exhausted":
        return "  … кап ходов — финальный вызов без тулов"
    if event == "agent.degrade":
        return f"✗ деградация: {payload.get('reason', '')}"
    if event == "agent.answer":
        return f"✓ ответ ({payload.get('source', '')})"
    if event in _TROUBLE_TEMPLATES:
        return _render_trouble(event, payload)
    return f"  · {event}"  # неизвестное событие — generic (устойчивость к будущим agent.*)


def _render_llm_complete(payload: dict[str, Any]) -> str:
    tokens = f"{payload.get('tokens_in', 0)}→{payload.get('tokens_out', 0)} ток"
    latency = f"{payload.get('latency_ms', 0) / _MS_PER_S:.1f}с"
    cost = f"{payload.get('cost_rub', 0.0):.2f}₽"
    line = f"  · {payload.get('model', '?')} · {tokens} · {latency} · {cost}"
    if payload.get("usage_missing"):
        line += " (шлюз без usage)"
    if payload.get("text_from_reasoning"):
        line += " [ответ из reasoning]"
    return line


def _render_observation(payload: dict[str, Any]) -> str:
    if not payload.get("ok", False):
        error_class = payload.get("error_class", "?")
        message = _truncate(" ".join(str(payload.get("error", "")).split()), _MESSAGE_MAX_CHARS)
        return f"  ← ОШИБКА {error_class}: {message}"
    truncated = "да" if payload.get("truncated") else "нет"
    header = f"  ← {payload.get('row_count', 0)} строк (усечён: {truncated})"
    columns = [str(column) for column in (payload.get("columns") or [])]
    rows = list(payload.get("rows_preview") or [])[:_PREVIEW_ROWS_MAX]
    if not columns:
        return header
    names = [_truncate(column, _CELL_MAX_CHARS) for column in columns]
    cells = [[_cell(row.get(column)) for column in columns] for row in rows]
    widths = [
        min(max([len(name), *(len(row[i]) for row in cells)]), _COLUMN_WIDTH_MAX)
        for i, name in enumerate(names)
    ]
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
    return "\n".join(lines)


def _render_trouble(event: str, payload: dict[str, Any]) -> str:
    payload = dict(payload)
    if event == "llm.breaker_open":
        payload["detail"] = payload.get("reason") or f"сбоев подряд: {payload.get('failures', '?')}"
    if event == "llm.budget_stop":
        payload["budget_used_rub"] = f"{payload.get('budget_used_rub', 0):.2f}"
        payload["cap_rub"] = f"{payload.get('cap_rub', 0):.2f}"
    return _TROUBLE_TEMPLATES[event].format(**payload)


class ConsoleTracer:
    """Адаптер `Tracer`: человекочитаемые этапы агента в stderr (best-effort)."""

    def __init__(self, stream: Any = None) -> None:
        # Инъекция потока — для тестов (capsys перехватывает системный stderr).
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
