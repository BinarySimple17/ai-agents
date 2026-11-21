"""Адаптер `EdgeProposalStore` — INSERT заявки в agent_edge_proposals (S09 Task 4).

Канал записи — ruling Q1 ARCH-GATE S09: DSN `agent_planner`; миграция 0009
выдала INSERT на таблицу + USAGE на serial-последовательность (без USAGE на
sequence INSERT падает с permission denied). SELECT/UPDATE/DELETE не выданы:
append-only через отсутствие грантов (прецедент 0002:92), без RETURNING —
заявка пишется вслепую, факт успеха = отсутствие исключения.

Заявка — settings-plane write кодом узла (не пользовательский SQL): MUST
«read-only agent_ro» не затрагивается; приватность «question_sample — чужие
вопросы» (0002) не задета — INSERT не читает чужие строки.
"""

import psycopg

from text_to_sql_agent.application.ports.edge_proposal_store import EdgeProposalError
from text_to_sql_agent.domain.entities import RunContext

# Приватность + размер: question_sample — пример вопроса без активного пути
# (ADR-006 §4.4), не переписка; усечение константой (ruling Q6; прецедент
# _ERROR_MESSAGE_MAX_CHARS=300 адаптера графа).
_QUESTION_SAMPLE_MAX_CHARS = 300

_INSERT_SQL = """
INSERT INTO agent_edge_proposals (left_table, right_table, question_sample)
VALUES (%s, %s, %s)
"""

_CONNECT_TIMEOUT_S = 10  # slow-фейл узла лучше висячего соединения (S00 правило 9)


def _shorten(message: str) -> str:
    message = " ".join(message.split())
    if len(message) <= _QUESTION_SAMPLE_MAX_CHARS:
        return message
    return message[: _QUESTION_SAMPLE_MAX_CHARS - 1] + "…"


class PgEdgeProposalStore:
    """Заявки в PostgreSQL под agent_planner; соединение на вызов (заявки редки)."""

    def __init__(self, planner_dsn: str) -> None:
        self._planner_dsn = planner_dsn

    def record_missing_edge(
        self,
        *,
        left_table: str,
        right_table: str,
        question_sample: str,
        ctx: RunContext,
    ) -> None:
        del ctx  # identity владельца очереди — planner-канал; trace_id — V4 (FSM-трейсинг)
        if not left_table or not right_table:
            raise ValueError("left_table/right_table обязательны")
        sample = _shorten(question_sample)
        if not sample:
            raise ValueError("question_sample пуст (нормализация whitespace)")
        try:
            with psycopg.connect(
                self._planner_dsn,
                connect_timeout=_CONNECT_TIMEOUT_S,
            ) as conn:
                conn.execute(_INSERT_SQL, (left_table, right_table, sample))
        except psycopg.Error as exc:
            raise EdgeProposalError(f"заявку записать не удалось: {_shorten(str(exc))}") from exc
