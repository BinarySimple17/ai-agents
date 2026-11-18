"""Integration-тесты схемы v1.5 (feat-002) — solution/sessions/S02_session.md.

Пункты behavior feat-002: идемпотентность alembic; состав таблиц v1.5; расширение
vector; границы ролей (agent_ro read-only whitelist, agent_trace INSERT-only,
registry_writer без DELETE); триггеры settings_version; вьюха active;
dblink/file_fdw не установлены; Neo4j по bolt с паролем из .env.
Скипаются без инфры с понятной причиной (conftest).
"""

import os
import uuid
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

BUSINESS_TABLES = (
    "customers",
    "campaigns",
    "orders",
    "order_items",
    "products",
    "employees",
    "bonuses",
)
AGENT_TABLES = (
    "agent_permissions",
    "agent_user_roles",
    "agent_relationships",
    "agent_edge_proposals",
    "agent_data_sources",
    "settings_version",
    "query_trace",
    "agent_audit",
)

# Демо-объёмы — зафиксированы в S02_session.md (Task 4, FIX-007):
# order_items = 1000 (1–4 позиции на заказ: Σ(1 + order_id % 4) по 400 заказам)
DEMO_COUNTS = {
    "customers": 20,
    "campaigns": 6,
    "products": 30,
    "orders": 400,
    "order_items": 1000,
    "employees": 12,
    "bonuses": 7,
}


def _dsn(env_var: str) -> str:
    dsn = os.getenv(env_var)
    if not dsn:
        pytest.skip(f"{env_var} не задан — скопируйте .env.example в .env")
    return dsn


@pytest.fixture(scope="function")
def admin_conn(postgres_available):
    with psycopg.connect(_dsn("POSTGRES_DSN")) as conn:
        yield conn


@pytest.fixture(scope="function")
def agent_ro_conn(postgres_available):
    with psycopg.connect(_dsn("AGENT_RO_DSN")) as conn:
        yield conn


@pytest.fixture(scope="function")
def trace_conn(postgres_available):
    with psycopg.connect(_dsn("AGENT_TRACE_DSN")) as conn:
        yield conn


@pytest.fixture(scope="function")
def writer_conn(postgres_available):
    """Соединение под registry_writer; креды собираются через make_conninfo.

    Пустой пароль при поднятой инфре — явный fail, а не skip: иначе зелёная
    верификация не отличает «граница проверена» от «граница пропущена» (ревью Minor-4).
    """
    from psycopg.conninfo import make_conninfo

    password = os.getenv("REGISTRY_WRITER_PASSWORD")
    if not password:
        pytest.fail(
            "REGISTRY_WRITER_PASSWORD не задан — скопируйте .env.example в .env: "
            "граница registry_writer не проверяема"
        )
    dsn = make_conninfo(_dsn("POSTGRES_DSN"), user="registry_writer", password=password)
    with psycopg.connect(dsn) as conn:
        yield conn


def test_alembic_upgrade_idempotent(postgres_available):
    """alembic upgrade head идемпотентен: повторный прогон не меняет версию (S02 Task 2)."""
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    # Программный вызов вместо subprocess: тест сам живёт под `uv run pytest`,
    # вложенный uv блокируется на lock окружения. env.py читает POSTGRES_DSN из
    # окружения — conftest уже подгрузил .env (fail-fast env.py срабатывает при
    # отсутствии .env — отдельный кейс, вне этого теста).
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    command.upgrade(cfg, "head")
    with psycopg.connect(_dsn("POSTGRES_DSN")) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        (version_after_first,) = cur.fetchone()
    command.upgrade(cfg, "head")
    with psycopg.connect(_dsn("POSTGRES_DSN")) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        (version_after_second,) = cur.fetchone()
    assert version_after_first == version_after_second == head


def test_schema_tables_exist(admin_conn):
    """Все таблицы схемы v1.5 существуют (7 бизнес + 8 служебных)."""
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        existing = {row[0] for row in cur.fetchall()}
    for table in (*BUSINESS_TABLES, *AGENT_TABLES):
        assert table in existing, f"таблица {table} отсутствует"


def test_active_view_exists(admin_conn):
    """Вьюха agent_relationships_active отдаёт только active-рёбра (ADR-006 §4.2)."""
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(*) FILTER (WHERE status <> 'active') "
            "FROM agent_relationships_active"
        )
        total, non_active = cur.fetchone()
        cur.execute("SELECT count(*) FROM agent_relationships WHERE status = 'active'")
        assert total == cur.fetchone()[0]
    assert non_active == 0


def test_vector_extension_installed(admin_conn):
    with admin_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        assert cur.fetchone() is not None, "расширение vector не загружено"


def test_no_dblink_file_fdw(admin_conn):
    """dblink/file_fdw не установлены — иначе обход whitelist валидатора (security §2.1)."""
    with admin_conn.cursor() as cur:
        cur.execute("SELECT extname FROM pg_extension WHERE extname IN ('dblink', 'file_fdw')")
        assert cur.fetchall() == []


def test_settings_version_bumped_by_triggers(admin_conn):
    """Любое изменение настроек инкрементирует счётчик (INSERT/DELETE — NFR-5, ADR-005 §4)."""
    with admin_conn.cursor() as cur:
        cur.execute("SELECT version FROM settings_version WHERE name = 'permissions'")
        (before,) = cur.fetchone()
        cur.execute(
            "INSERT INTO agent_permissions (role, table_name, columns, updated_by) "
            "VALUES ('__test_role__', '__test_table__', '*', 'test')"
        )
        cur.execute("SELECT version FROM settings_version WHERE name = 'permissions'")
        assert cur.fetchone()[0] == before + 1
        cur.execute(
            "DELETE FROM agent_permissions WHERE role = '__test_role__' "
            "AND table_name = '__test_table__'"
        )
        cur.execute("SELECT version FROM settings_version WHERE name = 'permissions'")
        assert cur.fetchone()[0] == before + 2
    admin_conn.rollback()


def _expect_denied(conn: psycopg.Connection, cur: psycopg.Cursor, sql: str) -> None:
    """Ожидаемый отказ прав (SQLSTATE 42501) + откат: отказ абортит транзакцию,
    без rollback следующий стейтмент упал бы InFailedSqlTransaction."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute(sql)  # noqa: S608 — SQL только из фиксированных списков в тестах
        cur.fetchall()
    conn.rollback()


def test_agent_ro_reads_business_only(agent_ro_conn):
    """agent_ro: SELECT бизнес-таблиц ок; query_trace и agent_* нечитаемы (whitelist, FIX-007)."""
    with agent_ro_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM orders")
        assert cur.fetchone()[0] == DEMO_COUNTS["orders"]
        for denied in ("query_trace", "agent_permissions", "agent_audit", "agent_relationships"):
            _expect_denied(agent_ro_conn, cur, f"SELECT * FROM {denied} LIMIT 1")


def test_agent_ro_read_only_session(agent_ro_conn):
    """agent_ro: INSERT/CREATE отклонены на уровне сессии (read-only — рубеж 5 ADR-005)."""
    with agent_ro_conn.cursor() as cur:
        for sql in (
            "INSERT INTO customers (name, country) VALUES ('x', 'RU')",
            "CREATE TABLE __ro_test__ (id int)",
        ):
            with pytest.raises(
                (psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)
            ):
                cur.execute(sql)
            agent_ro_conn.rollback()


def test_agent_trace_insert_only(trace_conn):
    """agent_trace: INSERT в query_trace ок; SELECT/UPDATE/DELETE не выдаются (security §2.2)."""
    with trace_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO query_trace (trace_id, user_id, question, status) "
            "VALUES (%s, 'u_test', 'тестовый прогон', 'ok')",
            (str(uuid.uuid4()),),
        )
        trace_conn.rollback()  # тестовые данные не оставляем (append-only таблица)
        for denied_sql in (
            "SELECT * FROM query_trace LIMIT 1",
            "UPDATE query_trace SET status = 'fail'",
            "DELETE FROM query_trace",
        ):
            # грант отсутствует (42501); если выдадут позже — сработает триггер (P0001
            # → RaiseException, который наследует ProgrammingError — ревью Minor-1)
            with pytest.raises(
                (psycopg.errors.InsufficientPrivilege, psycopg.errors.RaiseException)
            ):
                cur.execute(denied_sql)
                cur.fetchall()
            trace_conn.rollback()


def test_registry_writer_no_delete(writer_conn):
    """registry_writer: UPDATE ребра ок, DELETE запрещён — устаревание вместо удаления."""
    with writer_conn.cursor() as cur:
        # UPDATE без чтения колонок: WHERE/SET x = x требуют ещё и SELECT
        # (грант-семантика PostgreSQL), которого у writer нет — минимальный грант.
        # Без WHERE, с откатом: тест проверяет только UPDATE-грант.
        cur.execute("UPDATE agent_relationships SET usage_notes = 'touch'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("DELETE FROM agent_relationships WHERE edge_id = 'e_customers_orders'")
        writer_conn.rollback()


def test_bump_not_hijacked_by_temp_table(admin_conn):
    """SECURITY DEFINER bump пишет в public.settings_version, а не в temp-двойник
    сессии (ревью Important-1: pg_temp резолвится раньше public — иначе обходится
    инвариант NFR-5). Один транзакционный проход под админом: до/после/откат —
    функцию исполняет definer, caller-привилегии на разрешение имён не влияют."""
    with admin_conn.cursor() as cur:
        cur.execute("SELECT version FROM public.settings_version WHERE name = 'relationships'")
        (before,) = cur.fetchone()
        cur.execute("CREATE TEMP TABLE settings_version (name text PRIMARY KEY, version bigint)")
        cur.execute("UPDATE agent_relationships SET usage_notes = 'touch'")
        # реальный счётчик инкрементирован…
        cur.execute("SELECT version FROM public.settings_version WHERE name = 'relationships'")
        assert cur.fetchone()[0] == before + 1, (
            "bump ушёл в temp-двойник — счётчик версий обходится (NFR-5)"
        )
        # …а temp-двойник пуст (запись не перехвачена)
        cur.execute("SELECT count(*) FROM settings_version")
        assert cur.fetchone()[0] == 0
    admin_conn.rollback()  # UPDATE + bump откатываются — демо-данные не тронуты


def test_demo_data_traps(admin_conn):
    """Ловушки данных на месте: NULL campaign_id, не все премированы (er-data-model)."""
    with admin_conn.cursor() as cur:
        for table, expected in DEMO_COUNTS.items():
            cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 — имя из фиксированного списка
            assert cur.fetchone()[0] == expected, f"{table}: объём демо-данных изменился"
        cur.execute("SELECT count(*) FROM orders WHERE campaign_id IS NULL")
        assert cur.fetchone()[0] >= 30, "ловушка INNER JOIN потеряна (campaign_id NULL < 30)"
        cur.execute(
            "SELECT count(*) FROM employees e LEFT JOIN bonuses b ON b.employee_id = e.employee_id "
            "WHERE b.bonus_id IS NULL"
        )
        assert cur.fetchone()[0] >= 5, "ловушка LEFT JOIN потеряна (все премированы)"
        cur.execute(
            "SELECT count(*) FROM orders o WHERE o.total <> "
            "(SELECT COALESCE(SUM(oi.quantity * oi.unit_price), 0) "
            "FROM order_items oi WHERE oi.order_id = o.order_id)"
        )
        assert cur.fetchone()[0] == 0, "orders.total не сходится с позициями (ревью Minor-9)"


def test_neo4j_bolt_auth(neo4j_available):
    """Neo4j отвечает по bolt, аутентификация по NEO4J_PASSWORD из .env (behavior feat-002)."""
    from neo4j import GraphDatabase
    from neo4j.exceptions import AuthError, ServiceUnavailable

    uri = os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD")
    if not password:
        pytest.skip("NEO4J_PASSWORD не задан — скопируйте .env.example в .env")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
    except AuthError:
        pytest.fail("Neo4j отклонил креды из .env — проверьте NEO4J_PASSWORD")
    except ServiceUnavailable:
        pytest.skip("Neo4j bolt не поднялся — make infra-up")
    finally:
        driver.close()
