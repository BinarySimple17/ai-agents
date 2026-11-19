"""JSONL-трейсер (S03 Task 4; FR-11 node_timings) — лог вне git, путь из Settings.

Формат: одна запись JSON на строку; `ts` (ISO-8601 UTC) ставится при записи.
Запись потокобезопасна: оркестратор и узлы могут писать из разных потоков
(LangGraph-исполнение). Запись — best-effort (observability.md §2.2): сбой
файла (путь недоступен, диск полон) НЕ ломает оплаченный вызов LLM — проблема
фиксируется предупреждением, запрос продолжается (ревью S03 Important-2).
OTel-стек — ADR-010, вне скоупа S03.
"""

import json
import threading
import warnings
from datetime import UTC, datetime
from pathlib import Path

from text_to_sql_agent.domain.entities import Trace


class JsonlTracer:
    """Порт `Tracer` → append-only JSONL-файл (`.harness/traces/` — вне git)."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def record(self, trace: Trace) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            **trace.model_dump(),
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, self._path.open("a", encoding="utf-8") as fh:
                # default=str — psycopg отдаёт Decimal/date/datetime (регрессия S21
                # Critical-1: агентные события несут сырые rows_preview, без default
                # запись падала TypeError, --quiet ронял CLI, с TeeTracer событие
                # молча терялось из JSONL)
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            warnings.warn(
                f"JsonlTracer: запись не удалась ({exc}) — трейс потерян, запрос продолжается",
                RuntimeWarning,
                stacklevel=2,
            )
