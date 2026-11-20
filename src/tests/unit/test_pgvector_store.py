"""Юнит-тесты PgVectorStore на моках курсора (S06 Task 5; S00 правило 7 — без БД).

Проверяется форма SQL поиска: фильтр прав — джойн с настройками в самом запросе
(ADR-005 deny-by-default, не пост-фильтрация), инвариант по всем
referenced_tables (ревью S06 Critical-1), user_id — bind-параметром (данные, не
литералы SQL); контракты размерности (EMBEDDING_DIM), идемпотентный upsert по
source_ref, типизированная ошибка хранилища и разделение DSN-каналов
(search → planner, upsert → write; ruling W1). Поведение фильтра на живых
данных — интеграционные тесты test_vector_memory.py.
"""

import psycopg
import pytest

import text_to_sql_agent.infrastructure.memory.pgvector_store as pgvector_module
from text_to_sql_agent.application.ports.vector_store import (
    EMBEDDING_DIM,
    DocKind,
    KnowledgeDocument,
    VectorStoreError,
)
from text_to_sql_agent.domain.entities import RunContext
from text_to_sql_agent.infrastructure.memory.pgvector_store import (
    PgVectorStore,
    _to_pgvector,
)

_DSN = "postgresql://agent_planner:pw@127.0.0.1:5432/agent"
_WRITE_DSN = "postgresql://postgres:pw@127.0.0.1:5432/agent"
_VEC = [0.01] * EMBEDDING_DIM
_CTX = RunContext(trace_id="t-1", user_id="u_analyst")


class _FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict]:
        return list(self._rows)


class _FakeUpsertCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_FakeUpsertCursor":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def executemany(self, sql: str, params: list[tuple]) -> None:
        self._conn.executed.append((sql, params))


class _FakeConn:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.executed: list[tuple[str, object]] = []
        self.dsn: str | None = None

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None, prepare: bool = False):  # noqa: ARG002
        self.executed.append((sql, params))
        return _FakeCursor(self.rows)

    def cursor(self) -> _FakeUpsertCursor:
        return _FakeUpsertCursor(self)


def _row(i: int = 0, table: str | None = "orders", kind: str = "ddl") -> dict:
    return {
        "id": i,
        "doc_kind": kind,
        "table_name": table,
        "referenced_tables": [table] if table else [],
        "content": f"[таблица: {table}] контент {i}",
        "model_version": "text-embedding-bge-m3",
        "source_ref": f"ddl:{table}:{i}",
        "score": 0.87 - i * 0.1,
    }


def _connect_to(fake: _FakeConn):
    """connect-двойник, запоминающий DSN каждого вызова (каналы ruling W1)."""

    def connect(dsn: str, **kwargs: object) -> _FakeConn:
        fake.dsn = dsn
        return fake

    return connect


@pytest.fixture()
def fake_conn(monkeypatch: pytest.MonkeyPatch) -> _FakeConn:
    """Соединение-двойник: psycopg.connect → fake (мок курсора, без БД)."""
    conn = _FakeConn(
        [
            _row(0),
            _row(1, table="customers", kind="glossary"),
            _row(2, table=None, kind="few_shot"),
        ]
    )
    monkeypatch.setattr(pgvector_module.psycopg, "connect", _connect_to(conn))
    return conn


def _search_executed_of(fake_conn: _FakeConn) -> tuple[str, object]:
    # executed[0] — SET LOCAL statement_timeout; поисковый SQL — по таблице знаний
    return next(item for item in fake_conn.executed if "FROM agent_knowledge" in item[0])


def test_search_filters_by_rights_join_in_sql(fake_conn: _FakeConn) -> None:
    store = PgVectorStore(_DSN)
    store.search(_VEC, ctx=_CTX, top_k=5)
    sql, params = _search_executed_of(fake_conn)
    # Фильтр прав — джойн с настройками в самом SQL (deny-by-default), а не
    # пост-фильтрация выдачи (ADR-005/ADR-003 §4)
    assert "agent_user_roles" in sql and "agent_permissions" in sql
    # Инвариант по всем таблицам документа (ревью C1): unnest(referenced_tables)
    assert "unnest(referenced_tables)" in sql
    assert "referenced_tables = '{}'" in sql  # общие документы проходят всегда
    # user_id — bind-параметр, не литерал SQL (данные не вклеиваются в текст)
    assert _CTX.user_id not in sql
    assert params[1] == _CTX.user_id


def test_search_uses_search_dsn_channel(fake_conn: _FakeConn) -> None:
    # ruling W1: поиск — под ролью agent_planner (search_dsn), не под сервисной
    store = PgVectorStore(_DSN, write_dsn=_WRITE_DSN)
    store.search(_VEC, ctx=_CTX, top_k=5)
    assert fake_conn.dsn == _DSN


def test_upsert_uses_write_dsn_channel(fake_conn: _FakeConn) -> None:
    # ruling W1: запись — только офлайн-индексация под сервисным DSN
    store = PgVectorStore(_DSN, write_dsn=_WRITE_DSN)
    doc = KnowledgeDocument(
        doc_kind=DocKind.DDL,
        table_name="orders",
        referenced_tables=["orders"],
        content="[таблица: orders] DDL",
        model_version="text-embedding-bge-m3",
        source_ref="ddl:orders",
    )
    store.upsert([doc], [_VEC])
    assert fake_conn.dsn == _WRITE_DSN


def test_search_returns_documents_with_scores(fake_conn: _FakeConn) -> None:
    store = PgVectorStore(_DSN)
    docs = store.search(_VEC, ctx=_CTX, top_k=5)
    assert len(docs) == 3
    assert docs[0].doc_kind == DocKind.DDL
    assert docs[0].table_name == "orders"
    assert docs[0].referenced_tables == ["orders"]
    assert docs[1].doc_kind == DocKind.GLOSSARY
    assert docs[2].table_name is None
    assert docs[2].referenced_tables == []  # общий документ
    assert docs[0].score == pytest.approx(0.87)
    assert docs[2].source_ref == "ddl:None:2"


def test_search_sets_canonical_statement_timeout(fake_conn: _FakeConn) -> None:
    store = PgVectorStore(_DSN)
    store.search(_VEC, ctx=_CTX, top_k=5)
    assert any("statement_timeout" in sql for sql, _ in fake_conn.executed)


def test_search_wrong_dim_rejected() -> None:
    store = PgVectorStore(_DSN)
    with pytest.raises(ValueError, match="переиндексация"):
        store.search([0.1] * 768, ctx=_CTX, top_k=5)  # чужой эмбеддер


def test_search_top_k_must_be_positive() -> None:
    store = PgVectorStore(_DSN)
    with pytest.raises(ValueError, match="top_k"):
        store.search(_VEC, ctx=_CTX, top_k=0)


def test_storage_failure_wraps_into_vector_store_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Порт типизирован (прецедент SQLExecutionError): сырой psycopg.Error не
    # протекает в вызывающий слой (ревью S06 Important-3)
    class FailingConn:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def execute(self, *args: object, **kwargs: object) -> None:
            raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(pgvector_module.psycopg, "connect", lambda dsn, **kw: FailingConn())
    store = PgVectorStore(_DSN)
    with pytest.raises(VectorStoreError, match="векторная память недоступна"):
        store.search(_VEC, ctx=_CTX, top_k=5)


def test_upsert_requires_write_dsn() -> None:
    store = PgVectorStore(_DSN)
    doc = KnowledgeDocument(doc_kind=DocKind.DDL, content="x", model_version="m", source_ref="s")
    with pytest.raises(RuntimeError, match="write_dsn"):
        store.upsert([doc], [_VEC])


def test_upsert_sql_is_idempotent_by_source_ref(fake_conn: _FakeConn) -> None:
    store = PgVectorStore(_DSN, write_dsn=_WRITE_DSN)
    doc = KnowledgeDocument(
        doc_kind=DocKind.FEW_SHOT,
        table_name="orders",
        referenced_tables=["orders", "customers"],  # кросс-табличный пример
        content="[few-shot: orders] вопрос → SQL",
        model_version="text-embedding-bge-m3",
        source_ref="few_shot:orders:1",
    )
    assert store.upsert([doc], [_VEC]) == 1
    sql, params = fake_conn.executed[0]
    assert "ON CONFLICT (source_ref) DO UPDATE" in sql  # повторная индексация — update
    assert params[0] == (
        "few_shot",
        "orders",
        ["orders", "customers"],
        doc.content,
        _to_pgvector(_VEC),
        "text-embedding-bge-m3",
        "few_shot:orders:1",
    )


def test_upsert_length_mismatch_rejected() -> None:
    store = PgVectorStore(_DSN, write_dsn=_WRITE_DSN)
    doc = KnowledgeDocument(doc_kind=DocKind.DDL, content="x", model_version="m", source_ref="s")
    with pytest.raises(ValueError, match="параллельные списки"):
        store.upsert([doc], [])


def test_upsert_wrong_dim_rejected() -> None:
    store = PgVectorStore(_DSN, write_dsn=_WRITE_DSN)
    doc = KnowledgeDocument(doc_kind=DocKind.DDL, content="x", model_version="m", source_ref="s")
    with pytest.raises(ValueError, match="переиндексация"):
        store.upsert([doc], [[0.1] * 768])


def test_upsert_empty_batch_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    connected: list[str] = []
    monkeypatch.setattr(pgvector_module.psycopg, "connect", lambda dsn, **kw: connected.append(dsn))
    store = PgVectorStore(_DSN, write_dsn=_WRITE_DSN)
    assert store.upsert([], []) == 0
    assert connected == []  # пустой батч не открывает соединение
