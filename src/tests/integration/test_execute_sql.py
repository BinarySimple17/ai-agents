"""Integration-тесты канала исполнения SQL (feat-013) — solution/sessions/S13_session.md.

Пункты behavior feat-013: SELECT под `agent_ro` возвращает данные; LIMIT режет
>1000 строк (и ровно 1000 не помечается усечением); INSERT под `agent_ro` → отказ
БД (read-only сессии — рубеж 5); pg_sleep > 30s → ошибка по таймауту, не зависание;
`query_trace` недоступна `agent_ro` (whitelist-гранты); пустой результат — валидный
исход. RLS (миграция 0004, FIX-007): employees/bonuses отдают строки только при
выставленном `app.user_id` — fail-closed tripwire.
Скипаются без инфры с понятной причиной (conftest).
"""

import os

import psycopg
import pytest
from psycopg.rows import dict_row

from text_to_sql_agent.application.ports.sql_executor import (
    ROW_LIMIT,
    STATEMENT_TIMEOUT_S,
    QueryRequest,
    SQLExecutionError,
    SQLFailureClass,
)
from text_to_sql_agent.domain.entities import RunContext
from text_to_sql_agent.infrastructure.persistence.postgres_executor import PostgresExecutor

# Демо-объёмы — зафиксированы миграцией 0003 (DEMO_COUNTS в test_infra_schema)
EMPLOYEES_COUNT = 12
BONUSES_COUNT = 7
OVER_LIMIT_ROWS = 2500  # > ROW_LIMIT: генерируем series длиннее капа
TIMEOUT_PROBE_S = STATEMENT_TIMEOUT_S + 5  # pg_sleep дольше канонного timeout 30s
TRIPWIRE_USER_ID = "integration-agent"  # непустой user_id открывает tripwire


def _ro_dsn() -> str:
    dsn = os.getenv("AGENT_RO_DSN")
    if not dsn:
        pytest.skip("AGENT_RO_DSN не задан — скопируйте .env.example в .env")
    return dsn


@pytest.fixture(scope="session")
def ro_dsn(postgres_available) -> str:
    return _ro_dsn()


@pytest.fixture()
def executor(ro_dsn) -> PostgresExecutor:
    return PostgresExecutor(ro_dsn)


@pytest.fixture()
def ctx() -> RunContext:
    return RunContext(trace_id="integration-execute-sql", user_id=TRIPWIRE_USER_ID)


def test_select_under_agent_ro_returns_data(executor, ctx) -> None:
    result = executor.execute(QueryRequest(sql="SELECT name FROM customers LIMIT 5"), ctx=ctx)
    assert result.row_count == 5
    assert "name" in result.columns
    assert result.truncated is False


def test_limit_caps_over_limit_query(executor, ctx) -> None:
    sql = f"SELECT generate_series(1, {OVER_LIMIT_ROWS}) AS n"
    result = executor.execute(QueryRequest(sql=sql), ctx=ctx)
    assert result.row_count == ROW_LIMIT
    assert result.truncated is True


def test_exactly_at_limit_not_marked_truncated(executor, ctx) -> None:
    sql = f"SELECT generate_series(1, {ROW_LIMIT}) AS n"
    result = executor.execute(QueryRequest(sql=sql), ctx=ctx)
    assert result.row_count == ROW_LIMIT
    assert result.truncated is False  # пост-срез row_limit+1 отличает «ровно» от «срезано»


def test_user_limit_preserved_inside_wrapper(executor, ctx) -> None:
    # Ловушка 3: пользовательский LIMIT/OFFSET не глотается — работает внутри подзапроса
    result = executor.execute(QueryRequest(sql="SELECT * FROM order_items LIMIT 3"), ctx=ctx)
    assert result.row_count == 3
    assert result.truncated is False


def test_stacked_statements_rejected_before_execution(executor, ctx) -> None:
    # Регрессия на ревью S13 Critical-1: psycopg без prepare уходит в simple query
    # protocol и исполнял бы стек операторов — SET statement_timeout=0 снимает таймаут,
    # pg_sleep(1000) вешает канал. Extended protocol (prepare=True) обязан отвергнуть
    # multi-command ДО исполнения (ошибка syntax, без задержки)
    adversarial = "SELECT 1) AS q; SET statement_timeout = 0; SELECT pg_sleep(0); --"
    with pytest.raises(SQLExecutionError) as exc:
        executor.execute(QueryRequest(sql=adversarial), ctx=ctx)
    assert exc.value.error_class == SQLFailureClass.SYNTAX


def test_two_statements_rejected(executor, ctx) -> None:
    with pytest.raises(SQLExecutionError) as exc:
        executor.execute(QueryRequest(sql="SELECT 1; SELECT pg_sleep(0)"), ctx=ctx)
    assert exc.value.error_class == SQLFailureClass.SYNTAX


def test_trailing_semicolon_normalized(executor, ctx) -> None:
    # Ловушка 8: LLM-выдача регулярно вешает хвостовое ";" — для psql валидно,
    # внутри подзапроса обёртки нет; срез пустых операторов — не правка семантики
    result = executor.execute(QueryRequest(sql="SELECT COUNT(*) AS n FROM customers;"), ctx=ctx)
    assert result.row_count == 1
    assert result.columns == ["n"]


def test_executor_wrapper_is_structurally_select_only(executor, ctx) -> None:
    # Обёртка LIMIT завернёт только SELECT: плоский DML — SYNTAX по построению;
    # writable CTE PostgreSQL запрещает в подзапросе (FeatureNotSupported → syntax).
    # Второй слой — read-only сессия роли — проверяется прямым подключением ниже.
    with pytest.raises(SQLExecutionError) as exc:
        executor.execute(QueryRequest(sql="INSERT INTO customers SELECT * FROM customers"), ctx=ctx)
    assert exc.value.error_class == SQLFailureClass.SYNTAX


def test_agent_ro_session_is_read_only_at_db_level(ro_dsn) -> None:
    # Рубеж 5 (ADR-005): read-only проверен попыткой INSERT — отказ БД, не кода
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        with psycopg.connect(ro_dsn) as conn:
            # INSERT, который чисто планируется (identity-колонки не трогаем):
            # отказ — именно read-only сессия роли, не планирование
            conn.execute("INSERT INTO customers (name, country) VALUES ('probe', 'test')")


def test_query_trace_denied_to_agent_ro(executor, ctx) -> None:
    with pytest.raises(SQLExecutionError) as exc:
        executor.execute(QueryRequest(sql="SELECT * FROM query_trace"), ctx=ctx)
    assert exc.value.error_class == SQLFailureClass.PERMISSION_DENIED


def test_empty_result_is_valid_outcome_not_error(executor, ctx) -> None:
    result = executor.execute(QueryRequest(sql="SELECT * FROM customers WHERE false"), ctx=ctx)
    assert result.row_count == 0
    assert result.rows == []
    assert result.truncated is False
    assert result.columns  # структура известна, строк нет


def test_timeout_by_pg_sleep_not_hang(executor, ctx) -> None:
    # Канонный тест поведения: statement_timeout 30s срабатывает в БД, тест завершается
    sql = f"SELECT pg_sleep({TIMEOUT_PROBE_S})"
    with pytest.raises(SQLExecutionError) as exc:
        executor.execute(QueryRequest(sql=sql), ctx=ctx)
    assert exc.value.error_class == SQLFailureClass.TIMEOUT


def test_rls_employees_visible_with_user_id(executor, ctx) -> None:
    result = executor.execute(QueryRequest(sql="SELECT * FROM employees"), ctx=ctx)
    assert result.row_count == EMPLOYEES_COUNT


def test_rls_bonuses_visible_with_user_id(executor, ctx) -> None:
    result = executor.execute(QueryRequest(sql="SELECT * FROM bonuses"), ctx=ctx)
    assert result.row_count == BONUSES_COUNT


def test_rls_tripwire_zero_rows_without_user_id(ro_dsn) -> None:
    # Прямое подключение agent_ro БЕЗ set_config('app.user_id'): tripwire молчит — 0 строк
    with psycopg.connect(ro_dsn, row_factory=dict_row) as conn:
        row = conn.execute("SELECT count(*) AS n FROM employees").fetchone()
    assert row["n"] == 0
