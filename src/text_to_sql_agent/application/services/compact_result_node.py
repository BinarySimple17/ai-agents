"""Узел compact_result для FSM (feat-015, S15) — glue сервис ↔ state.

Код-узел (hw-01 §3.4: compact_result — код, без LLM): сворачивает state.rows в
компактный вид (domain.compact_rows) и кладёт в state.compact — карта владения
(`compact` — компакт_result). LLM-бюджет прогона не платит (гвард капов на
входящем ребре не нужен — узел не в LLM_NODES). Сырые строки дальше не идут:
interpret_result строит объяснение ТОЛЬКО на компактном виде (behavior feat-015).
"""

from typing import Any

from text_to_sql_agent.domain.result_compact import compact_rows

State = dict[str, Any]

ROWS_KEY = "rows"
COMPACT_KEY = "compact"


def make_compact_result_node(*, tracer: Any | None = None) -> Any:
    """Фабрика узла compact_result: компакция кодом (tracer — единообразие фабрик)."""

    def node(state: State) -> dict[str, Any]:
        compact = compact_rows(
            state.get(ROWS_KEY),
            truncated=bool(state.get("truncated")),
            approximate=bool(state.get("approximate")),
        )
        return {COMPACT_KEY: compact.model_dump()}

    return node
