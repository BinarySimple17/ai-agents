"""Юнит-тесты PgVectorStore на моках курсора (S06 Task 5, S26 Task 3; S00 правило 7 — без БД).

Проверяется форма SQL поиска: фильтр прав — джойн с настройками в самом запросе
ОБЕИХ ног ДО слияния (ADR-005 deny-by-default, NFR-4; Ловушки-2 S26), инвариант
по всем referenced_tables (ревью S06 Critical-1), user_id — bind-параметром
(данные, не литералы SQL); контракты размерности (EMBEDDING_DIM), replace-
семантика upsert (ruling N1 S26: upsert + удаление устаревших неймспейсов одной
транзакцией), нормализация контента при записи (D-026 доп. 1(2)), фильтр
model_version (BL-009 M3), типизированная ошибка хранилища и разделение
DSN-каналов (search → planner, upsert → write; ruling W1). Формула взвешенного
RRF — на чистой функции _rrf_merge (якорь BEIR D-026: dense-ранг-1 бьёт
FTS-only-ранг-1). Поведение фильтра на живых данных — integration
test_vector_memory.py / test_hybrid_memory.py.
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
    MAX_LEG_DEPTH,
    RRF_ALPHA_DENSE,
    RRF_K,
    PgVectorStore,
    _rrf_merge,
    _to_pgvector,
)

_DSN = "postgresql://agent_planner:pw@127.0.0.1:5432/agent"
_WRITE_DSN = "postgresql://postgres:pw@127.0.0.1:5432/agent"
_VEC = [0.01] * EMBEDDING_DIM
_QUERY = "выручка по заказам"
_CTX = RunContext(trace_id="t-1", user_id="u_analyst")

# Маркеры SQL ног на двойнике соединения: dense-нога содержит оператор cosine,
# FTS-нога — plainto_tsquery (запрос текста без вектора)
_DENSE_MARKER = "embedding <=>"
_FTS_MARKER = "plainto_tsquery"


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

    def execute(self, sql: str, params: object = None) -> None:
        self._conn.executed.append((sql, params))


class _FakeConn:
    def __init__(
        self,
        rows: list[dict] | None = None,
        rows_by_marker: dict[str, list[dict]] | None = None,
    ) -> None:
        self.rows = rows or []
        self.rows_by_marker = rows_by_marker or {}
        self.executed: list[tuple[str, object]] = []
        self.dsn: str | None = None

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None, prepare: bool = False):  # noqa: ARG002
        self.executed.append((sql, params))
        rows = self.rows
        for marker, marker_rows in self.rows_by_marker.items():
            if marker in sql:
                rows = marker_rows
                break
        return _FakeCursor(rows)

    def cursor(self) -> _FakeUpsertCursor:
        return _FakeUpsertCursor(self)


def _row(
    i: int = 0, table: str | None = "orders", kind: str = "ddl", ref: str | None = None
) -> dict:
    return {
        "id": i,
        "doc_kind": kind,
        "table_name": table,
        "referenced_tables": [table] if table else [],
        "content": f"[таблица: {table}] контент {i}",
        "model_version": "text-embedding-bge-m3",
        "source_ref": ref or f"ddl:{table}:{i}",
        "leg_score": 0.87 - i * 0.1,
    }


def _doc(ref: str, *, table: str | None = "orders", kind: str = "ddl") -> KnowledgeDocument:
    return KnowledgeDocument(
        doc_kind=DocKind(kind),
        table_name=table,
        referenced_tables=[table] if table else [],
        content=f"контент {ref}",
        model_version="text-embedding-bge-m3",
        source_ref=ref,
    )


def _connect_to(fake: _FakeConn):
    """connect-двойник, запоминающий DSN каждого вызова (каналы ruling W1)."""

    def connect(dsn: str, **kwargs: object) -> _FakeConn:
        fake.dsn = dsn
        return fake

    return connect


@pytest.fixture()
def fake_conn(monkeypatch: pytest.MonkeyPatch) -> _FakeConn:
    """Соединение-двойник: psycopg.connect → fake (мок курсора, без БД).

    Обе ноги поиска получают одинаковый список строк — документ, найденный
    обеими ногами на том же ранге, получает score = α/(K+rank) + (1−α)/(K+rank).
    """
    legs_rows = [
        _row(0),
        _row(1, table="customers", kind="glossary"),
        _row(2, table=None, kind="few_shot"),
    ]
    conn = _FakeConn(rows_by_marker={_DENSE_MARKER: legs_rows, _FTS_MARKER: legs_rows})
    monkeypatch.setattr(pgvector_module.psycopg, "connect", _connect_to(conn))
    return conn


def _executed_legs_of(fake_conn: _FakeConn) -> list[tuple[str, object]]:
    return [item for item in fake_conn.executed if "FROM agent_knowledge" in item[0]]


def test_search_filters_by_rights_join_in_sql_of_both_legs(fake_conn: _FakeConn) -> None:
    store = PgVectorStore(_DSN)
    store.search(_QUERY, _VEC, ctx=_CTX, top_k=5)
    legs = _executed_legs_of(fake_conn)
    assert len(legs) == 2, "гибридный поиск — две ноги на одном соединении"
    for sql, params in legs:
        # Фильтр прав — джойн с настройками в самом SQL (deny-by-default), а не
        # пост-фильтрация выдачи (ADR-005/ADR-003 §4; NFR-4: ДО слияния)
        assert "agent_user_roles" in sql and "agent_permissions" in sql
        assert "unnest(" in sql  # инвариант по всем таблицам (ревью C1; FTS — k.квалификация)
        assert "= '{}'" in sql  # общие документы проходят всегда
        assert _CTX.user_id not in sql  # user_id — bind-параметр, не литерал SQL
        assert params[1] == _CTX.user_id
    assert any(_DENSE_MARKER in sql for sql, _ in legs), "dense-нога: cosine"
    assert any(_FTS_MARKER in sql for sql, _ in legs), "FTS-нога: plainto_tsquery"


def test_search_ranks_by_weighted_rrf(fake_conn: _FakeConn) -> None:
    # Один документ найден обеими ногами на ранге r → score = α/(K+r) + (1−α)/(K+r)
    store = PgVectorStore(_DSN)
    docs = store.search(_QUERY, _VEC, ctx=_CTX, top_k=3)
    assert len(docs) == 3
    expected_top = RRF_ALPHA_DENSE / (RRF_K + 1) + (1 - RRF_ALPHA_DENSE) / (RRF_K + 1)
    assert docs[0].score == pytest.approx(expected_top)
    assert docs[1].score == pytest.approx(
        RRF_ALPHA_DENSE / (RRF_K + 2) + (1 - RRF_ALPHA_DENSE) / (RRF_K + 2)
    )
    assert [d.source_ref for d in docs] == [
        "ddl:orders:0",
        "ddl:customers:1",
        "ddl:None:2",
    ]


def test_search_top_k_slices_after_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ранги RRF считаются по глубине ноги (MAX_LEG_DEPTH), срез top_k — после
    # слияния (ruling N3): короткие ноги сломали бы ранжирование
    conn = _FakeConn(
        rows_by_marker={
            _DENSE_MARKER: [_row(i) for i in range(6)],
            _FTS_MARKER: [_row(i) for i in range(6)],
        }
    )
    monkeypatch.setattr(pgvector_module.psycopg, "connect", _connect_to(conn))
    store = PgVectorStore(_DSN)
    docs = store.search(_QUERY, _VEC, ctx=_CTX, top_k=2)
    assert [d.source_ref for d in docs] == ["ddl:orders:0", "ddl:orders:1"]
    # Кап глубины — на ОБОИХ ногах (m15: регресс замены шаблона одной ноги не поймать)
    for marker in (_DENSE_MARKER, _FTS_MARKER):
        leg_sql = next(sql for sql, _ in conn.executed if marker in sql)
        assert f"LIMIT {MAX_LEG_DEPTH}" in leg_sql


def test_rrf_merge_dense_rank_one_beats_fts_only_rank_one() -> None:
    # Якорь BEIR из D-026 доп. 1(1): при α=0.9 документ dense-ранга-1 бьёт
    # FTS-only-ранг-1 (невзвешенный RRF дал бы им одинаковый 1/61)
    dense = [_doc("dense-a"), _doc("dense-b")]
    fts = [_doc("fts-only")]
    merged = _rrf_merge(dense, fts, alpha_dense=RRF_ALPHA_DENSE)
    assert [d.source_ref for d in merged] == ["dense-a", "dense-b", "fts-only"]
    assert merged[0].score == pytest.approx(RRF_ALPHA_DENSE / (RRF_K + 1))
    assert merged[2].score == pytest.approx((1 - RRF_ALPHA_DENSE) / (RRF_K + 1))


def test_rrf_merge_both_legs_doc_wins_over_dense_rank_one() -> None:
    # Документ, найденный обеими ногами (dense ранг 2 + FTS ранг 1), обгоняет
    # dense-only-ранг-1: сумма вкладов перевешивает одиночный ранг слабой ноги
    merged = _rrf_merge([_doc("dense-a"), _doc("b")], [_doc("b")], alpha_dense=RRF_ALPHA_DENSE)
    scores = {d.source_ref: d.score for d in merged}
    assert scores["b"] == pytest.approx(
        RRF_ALPHA_DENSE / (RRF_K + 2) + (1 - RRF_ALPHA_DENSE) / (RRF_K + 1)
    )
    assert merged[0].source_ref == "b"
    assert merged[1].source_ref == "dense-a"


def test_rrf_merge_tie_break_is_deterministic() -> None:
    # Точная ничья возможна при α=0.5 (симметричный обмен рангами): порядок
    # определяется source_ref, не порядком ног
    first = _rrf_merge([_doc("x"), _doc("y")], [_doc("y"), _doc("x")], alpha_dense=0.5)
    second = _rrf_merge([_doc("y"), _doc("x")], [_doc("x"), _doc("y")], alpha_dense=0.5)
    assert [d.source_ref for d in first] == [d.source_ref for d in second]
    assert first[0].score == pytest.approx(first[1].score)


def test_rrf_alpha_dense_validated() -> None:
    with pytest.raises(ValueError, match="rrf_alpha_dense"):
        PgVectorStore(_DSN, rrf_alpha_dense=1.5)


def test_search_uses_search_dsn_channel(fake_conn: _FakeConn) -> None:
    # ruling W1: поиск — под ролью agent_planner (search_dsn), не под сервисной
    store = PgVectorStore(_DSN, write_dsn=_WRITE_DSN)
    store.search(_QUERY, _VEC, ctx=_CTX, top_k=5)
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


def test_search_sets_canonical_statement_timeout(fake_conn: _FakeConn) -> None:
    store = PgVectorStore(_DSN)
    store.search(_QUERY, _VEC, ctx=_CTX, top_k=5)
    assert any("statement_timeout" in sql for sql, _ in fake_conn.executed)


def test_document_anchor_must_be_declared() -> None:
    # Defense-in-depth (ревью-2): якорь без объявления -> '{}' сделал бы документ публичным
    with pytest.raises(ValueError, match="не объявлена в referenced_tables"):
        KnowledgeDocument(
            doc_kind=DocKind.DDL,
            table_name="orders",
            content="x",
            model_version="m",
            source_ref="s",
        )
    common = KnowledgeDocument(
        doc_kind=DocKind.GLOSSARY, content="x", model_version="m", source_ref="s"
    )
    assert common.table_name is None and common.referenced_tables == []  # общий документ валиден


def test_noise_doc_kind_exists() -> None:
    # S26: дистрактор — аддитивное значение enum (D-026; ruling N2 —
    # referenced_tables по упоминаниям, guard универсален)
    noise = KnowledgeDocument(
        doc_kind=DocKind.NOISE,
        referenced_tables=["orders"],
        content="[шум: orders] дистрактор",
        model_version="m",
        source_ref="noise:0001",
    )
    assert noise.table_name is None and noise.referenced_tables == ["orders"]


def test_search_wrong_dim_rejected() -> None:
    store = PgVectorStore(_DSN)
    with pytest.raises(ValueError, match="переиндексация"):
        store.search(_QUERY, [0.1] * 768, ctx=_CTX, top_k=5)  # чужой эмбеддер


def test_search_top_k_must_be_positive() -> None:
    store = PgVectorStore(_DSN)
    with pytest.raises(ValueError, match="top_k"):
        store.search(_QUERY, _VEC, ctx=_CTX, top_k=0)


def test_search_empty_query_rejected() -> None:
    # FTS-ноге нужен текст запроса — пустой запрос нарушает контракт стыка
    store = PgVectorStore(_DSN)
    with pytest.raises(ValueError, match="query пуст"):
        store.search("   ", _VEC, ctx=_CTX, top_k=5)


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
        store.search(_QUERY, _VEC, ctx=_CTX, top_k=5)


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
    sql, params = next(item for item in fake_conn.executed if "ON CONFLICT" in item[0])
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


def test_upsert_normalizes_content_before_write(fake_conn: _FakeConn) -> None:
    # Нормализация ДО записи (D-026 доп. 1(2)): generated-колонка content_tsv
    # берёт content как есть — акут U+0301/ё→е снимает хелпер, не конфиг SQL
    store = PgVectorStore(_DSN, write_dsn=_WRITE_DSN)
    doc = KnowledgeDocument(
        doc_kind=DocKind.GLOSSARY,
        content="[термин: объём] Кана́рские ёлки",
        model_version="m",
        source_ref="glossary:тест",
    )
    store.upsert([doc], [_VEC])
    _, params = next(item for item in fake_conn.executed if "ON CONFLICT" in item[0])
    assert params[0][3] == "[термин: объем] Канарские елки"


def test_upsert_deletes_stale_refs_of_reindexed_namespaces(fake_conn: _FakeConn) -> None:
    # BL-009 M3 (ruling N1): устаревшие source_ref удаляются ТОЙ ЖЕ транзакцией;
    # скоуп — неймспейсы (префикс до ':') переданной коллекции: живой корпус и
    # сиды тестов в одной таблице не затирают друг друга
    store = PgVectorStore(_DSN, write_dsn=_WRITE_DSN)
    docs = [
        KnowledgeDocument(
            doc_kind=DocKind.DDL,
            content="x",
            model_version="m",
            source_ref="ddl:orders",
        ),
        KnowledgeDocument(
            doc_kind=DocKind.NOISE,
            referenced_tables=["orders"],
            content="y",
            model_version="m",
            source_ref="noise:0001",
        ),
    ]
    store.upsert(docs, [_VEC, _VEC])
    delete_sql, delete_params = fake_conn.executed[-1]
    assert "DELETE FROM agent_knowledge" in delete_sql
    assert "split_part(source_ref, ':', 1)" in delete_sql
    assert delete_params == (["ddl", "noise"], ["ddl:orders", "noise:0001"])


def test_upsert_empty_batch_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Replace-семантика: пустой вызов = «стери неймспейсы» — footgun, fail-fast
    connected: list[str] = []
    monkeypatch.setattr(pgvector_module.psycopg, "connect", lambda dsn, **kw: connected.append(dsn))
    store = PgVectorStore(_DSN, write_dsn=_WRITE_DSN)
    with pytest.raises(ValueError, match="replace-семантике"):
        store.upsert([], [])
    assert connected == []  # соединение не открывается


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


def test_search_normalizes_query_text_for_fts_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    # I4b ревью S26: normalize_text(query) на FTS-ноге обязателен (D-026 доп. 1(2) —
    # акут U+0301 режет термы plainto_tsquery пополам); регресс на удаление вызова
    conn = _FakeConn(rows_by_marker={_DENSE_MARKER: [_row(0)], _FTS_MARKER: [_row(0)]})
    monkeypatch.setattr(pgvector_module.psycopg, "connect", _connect_to(conn))
    store = PgVectorStore(_DSN)
    store.search("Объём вы́ручки", _VEC, ctx=_CTX, top_k=5)
    fts_sql, fts_params = next(item for item in conn.executed if _FTS_MARKER in item[0])
    assert "plainto_tsquery" in fts_sql
    assert fts_params[0] == "Объем выручки"  # акут снят, ё→е; регистр не тронут


def test_search_filters_by_model_version_when_expected(monkeypatch: pytest.MonkeyPatch) -> None:
    # BL-009 M3: смена эмбеддера — строки чужого векторного пространства не
    # смешиваются с текущим (фильтр model_version в SQL обеих ног)
    conn = _FakeConn(
        rows_by_marker={
            _DENSE_MARKER: [_row(0)],
            _FTS_MARKER: [_row(0)],
        }
    )
    monkeypatch.setattr(pgvector_module.psycopg, "connect", _connect_to(conn))
    store = PgVectorStore(_DSN, expected_model_version="text-embedding-bge-m3")
    store.search(_QUERY, _VEC, ctx=_CTX, top_k=5)
    for sql, params in _executed_legs_of(conn):
        assert "model_version = %s" in sql
        assert params[2] == "text-embedding-bge-m3"


def test_search_without_expected_model_version_has_no_filter(fake_conn: _FakeConn) -> None:
    # Дефолт (None) — фильтра нет: синтетические корпуса тестов видимы
    store = PgVectorStore(_DSN)
    store.search(_QUERY, _VEC, ctx=_CTX, top_k=5)
    for sql, params in _executed_legs_of(fake_conn):
        assert "model_version = %s" not in sql
        assert params[1] == _CTX.user_id  # параметры — только вектор/пользователь
