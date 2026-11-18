"""Порт `Tracer` — журнал событий узлов/адаптеров (FR-11 node_timings; S00 таблица портов).

Реализация S03 — `infrastructure/observability/jsonl_tracer.py` (JSONL вне git,
путь из `Settings.trace_jsonl_path`); стек OTel — ADR-010, вне скоупа S03.
"""

from typing import Protocol, runtime_checkable

from text_to_sql_agent.domain.entities import Trace


@runtime_checkable
class Tracer(Protocol):
    def record(self, trace: Trace) -> None:
        """Записать одно событие наблюдаемости (fmt-нейтрально для адаптера OTel)."""
        ...
