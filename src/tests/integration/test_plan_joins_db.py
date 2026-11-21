"""Integration-тесты очереди заявок agent_edge_proposals (feat-009 Task 4; BL-003 п.2).

Ruling Q1 ARCH-GATE S09: заявка пишется кодом узла под ролью agent_planner
(миграция 0009: INSERT + USAGE ON SEQUENCE); append-only — UPDATE/SELECT/DELETE
у planner нет (приватность «question_sample — чужие вопросы» из 0002);
agent_ro (канал исполнения SQL) записывать не может — «LLM-путь исключён»:
заявка — settings-plane write, не пользовательский SQL. Скипаются без инфры
(conftest postgres_available).

Реестр тестами не засоряется: созданные строки удаляются в finally под
сервисным DSN (владелец очереди в тестах — админ-канал миграций).
"""

import psycopg
import pytest
from psycopg.rows import dict_row

from text_to_sql_agent.config.settings import load_settings
from text_to_sql_agent.domain.entities import RunContext
from text_to_sql_agent.infrastructure.persistence.edge_proposal_store import PgEdgeProposalStore

CTX = RunContext(trace_id="it-plan-joins-db", user_id="u_logist")
_QUESTION = "unit-plan-joins: вопрос без активного пути между сущностями"


@pytest.fixture(scope="module")
def settings(postgres_available):
    """Доступность инфры ДО load_settings() (ревью S07 Critical-1 — без Docker скип)."""
    return load_settings()


@pytest.fixture(scope="module")
def proposal_store(settings):
    return PgEdgeProposalStore(settings.agent_planner_dsn)


@pytest.fixture(scope="module")
def cleanup(settings):
    """Чистка созданных строк после модуля — очередь владельцу не засоряем."""

    def _cleanup() -> int:
        with psycopg.connect(
            settings.postgres_dsn.get_secret_value(), row_factory=dict_row
        ) as conn:
            row = conn.execute(
                "DELETE FROM agent_edge_proposals WHERE question_sample LIKE %s "
                "RETURNING proposal_id",
                ("unit-plan-joins%",),
            ).fetchall()
            conn.commit()
            return len(row)

    return _cleanup


def _admin_query(settings, sql: str, params: tuple = ()):
    with psycopg.connect(settings.postgres_dsn.get_secret_value(), row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def test_proposal_inserted_under_planner_role(settings, proposal_store, cleanup) -> None:
    """Заявка пишется под agent_planner (INSERT + USAGE SEQUENCE из миграции 0009)."""
    try:
        proposal_store.record_missing_edge(
            left_table="customers",
            right_table="bonuses",
            question_sample=_QUESTION,
            ctx=CTX,
        )
        rows = _admin_query(
            settings,
            "SELECT left_table, right_table, question_sample, hits, status "
            "FROM agent_edge_proposals WHERE question_sample LIKE %s",
            ("unit-plan-joins%",),
        )
        assert len(rows) == 1
        assert rows[0]["left_table"] == "customers"
        assert rows[0]["right_table"] == "bonuses"
        assert rows[0]["hits"] == 1  # агент hits не инкрементит — частота = COUNT(*) (ruling Q1)
        assert rows[0]["status"] == "open"
    finally:
        assert cleanup() == 1


def test_question_sample_truncated_to_constant(settings, proposal_store, cleanup) -> None:
    """question_sample усекается константой адаптера (приватность + размер, ruling Q6)."""
    long_question = "unit-plan-joins: " + ("очень длинный вопрос " * 30)
    try:
        proposal_store.record_missing_edge(
            left_table="orders", right_table="bonuses", question_sample=long_question, ctx=CTX
        )
        rows = _admin_query(
            settings,
            "SELECT question_sample FROM agent_edge_proposals WHERE question_sample LIKE %s",
            ("unit-plan-joins%",),
        )
        assert len(rows) == 1
        assert len(rows[0]["question_sample"]) == 300
        assert rows[0]["question_sample"].endswith("…")
    finally:
        assert cleanup() == 1


def test_planner_is_append_only(settings, proposal_store) -> None:
    """У planner только INSERT: UPDATE/SELECT не выданы (append-only через гранты)."""
    with psycopg.connect(settings.agent_planner_dsn) as conn:
        # Ожидаемая ошибка абортит транзакцию — rollback между проверками
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE agent_edge_proposals SET hits = 2")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM agent_edge_proposals")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM agent_edge_proposals")


def test_agent_ro_cannot_write_proposals(settings) -> None:
    """LLM-путь исключён: заявка — settings-plane write кодом узла, не пользовательский SQL
    (канал исполнения agent_ro остаётся read-only, hw-01 §3.2): сессионный
    read-only (FIX-007) ловит INSERT раньше грантов."""
    with psycopg.connect(settings.agent_ro_dsn.get_secret_value()) as conn:
        denied = (psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)
        with pytest.raises(denied):
            conn.execute(
                "INSERT INTO agent_edge_proposals (left_table, right_table, question_sample) "
                "VALUES ('x', 'y', 'unit-plan-joins: попытка обхода')"
            )
