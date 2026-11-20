"""Integration-тесты векторной памяти (feat-006, S06) — команда верификации фичи.

behavior feat-006: поиск по эмбеддингу возвращает только документы разрешённых
таблиц; в выдаче есть глоссарий и few-shot релевантных таблиц; недоступные
таблицы не попадают в контекст даже при близком совпадении эмбеддинга
(NFR-4 ACL-leak = 0). Фильтр прав — джойн в SQL поиска (ADR-005 deny-by-default,
не пост-фильтрация), инвариант по всем referenced_tables (ревью S06 C1);
чтение — роль agent_planner, запись — сервисный DSN (ruling W1 ARCH-GATE S06).
Скипаются без инфры с понятной причиной (conftest).

Эмбеддинги в тестах — синтетические (S00 правило 7: реальный шлюз в тестах не
вызывается): ортонормированные 1024-мерные векторы образуют «кластеры» таблиц
(cosine 0 между кластерами) — релевантность определяется вектором запроса, а
эффект фильтра прав — чисто табличным джойном. Сид — через идемпотентный upsert
адаптера (попутно покрывает upsert на живой БД); очистка — по префиксу source_ref.
"""

import re

import psycopg
import pytest

from text_to_sql_agent.application.ports.vector_store import DocKind, KnowledgeDocument
from text_to_sql_agent.config.settings import load_settings
from text_to_sql_agent.domain.entities import RunContext
from text_to_sql_agent.infrastructure.memory.pgvector_store import PgVectorStore

_REF_PREFIX = "it-vector:"
_DIM = 1024
_MODEL = "synthetic-test-embedder"

# Демо-права (миграция 0003): analyst — orders/order_items/products;
# hr_viewer — bonuses/employees; права отсутствуют только у неизвестных user_id
CTX_ANALYST = RunContext(trace_id="it-vector", user_id="u_analyst")
CTX_HR = RunContext(trace_id="it-vector", user_id="u_hr")
CTX_UNKNOWN = RunContext(trace_id="it-vector", user_id="it-nobody")


def _unit(i: int) -> list[float]:
    """Орт i: кластер-вектор таблицы; между ортами cosine 0 — кластеры не смешиваются."""
    return [1.0 if j == i else 0.0 for j in range(_DIM)]


V_ORDERS = _unit(0)
V_BONUSES = _unit(1)  # недоступная analyst'у таблица — отдельный кластер
V_COMMON = [0.7071 if j in (0, 1) else 0.0 for j in range(_DIM)]


def _seed_docs() -> tuple[list[KnowledgeDocument], list[list[float]]]:
    def doc(
        kind: DocKind,
        tables: list[str],
        content: str,
        ref: str,
    ) -> KnowledgeDocument:
        return KnowledgeDocument(
            doc_kind=kind,
            table_name=tables[0] if tables else None,
            referenced_tables=tables,
            content=content,
            model_version=_MODEL,
            source_ref=f"{_REF_PREFIX}{ref}",  # префикс — ключ очистки тестового сида
        )

    docs = [
        doc(
            DocKind.DDL,
            ["orders"],
            "[таблица: orders] DDL: orders(order_id, customer_id, total)",
            "ddl-orders",
        ),
        doc(
            DocKind.DDL,
            ["bonuses"],
            "[таблица: bonuses] DDL: bonuses(bonus_id, employee_id, amount)",
            "ddl-bonuses",
        ),
        doc(
            DocKind.GLOSSARY,
            ["orders"],
            "[термин: выручка-test] выручка = SUM(orders.total)",
            "glossary-orders",
        ),
        doc(
            DocKind.FEW_SHOT,
            ["orders"],
            "[few-shot: test] Сколько заказов? → SELECT COUNT(*) FROM orders",
            "few-shot-orders",
        ),
        doc(
            DocKind.GLOSSARY,
            [],
            "[термин: общий-test] термин без привязки к таблице",
            "glossary-common",
        ),
        # Регресс ревью S06 C1: документ привязан к разрешённой analyst'у orders,
        # но упоминает недоступную bonuses — скрыт по referenced_tables целиком
        doc(
            DocKind.FEW_SHOT,
            ["orders", "bonuses"],
            "[few-shot: кросс-test] выручка и премии: orders.total + bonuses.amount",
            "few-shot-cross",
        ),
    ]
    vectors = [V_ORDERS, V_BONUSES, V_ORDERS, V_ORDERS, V_COMMON, V_ORDERS]
    return docs, vectors


def _cleanup(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(f"DELETE FROM agent_knowledge WHERE source_ref LIKE '{_REF_PREFIX}%'")


@pytest.fixture()
def store(postgres_available) -> PgVectorStore:
    settings = load_settings()
    return PgVectorStore(
        settings.agent_planner_dsn,
        write_dsn=settings.postgres_dsn.get_secret_value(),
    )


@pytest.fixture()
def seeded(store) -> None:
    service_dsn = load_settings().postgres_dsn.get_secret_value()
    _cleanup(service_dsn)  # на случай незачищенного предыдущего прогона
    docs, vectors = _seed_docs()
    assert store.upsert(docs, vectors) == len(docs)
    yield
    _cleanup(service_dsn)


def _refs(docs: list[KnowledgeDocument]) -> set[str]:
    return {d.source_ref for d in docs}


def test_upsert_is_idempotent_by_source_ref(store) -> None:
    docs, vectors = _seed_docs()
    service_dsn = load_settings().postgres_dsn.get_secret_value()
    try:
        assert store.upsert(docs, vectors) == len(docs)

        def _seeded_count() -> int:
            with psycopg.connect(service_dsn) as conn:
                row = conn.execute(
                    "SELECT count(*) FROM agent_knowledge WHERE source_ref LIKE %s",
                    (f"{_REF_PREFIX}%",),
                ).fetchone()
            return row[0]

        assert _seeded_count() == len(docs)
        assert store.upsert(docs, vectors) == len(docs)  # повторный сид — update, не дубль
        assert _seeded_count() == len(docs)
    finally:
        _cleanup(service_dsn)


def test_search_returns_only_allowed_tables(store, seeded) -> None:
    docs = store.search(V_ORDERS, ctx=CTX_ANALYST, top_k=50)
    tables = {t for d in docs for t in d.referenced_tables}
    assert "bonuses" not in tables  # недоступная таблица — не существует в мире агента
    assert "employees" not in tables  # и вторая HR-таблица тоже
    assert f"{_REF_PREFIX}ddl-orders" in _refs(docs)  # разрешённая — в выдаче


def test_glossary_and_few_shot_surface_for_relevant_tables(store, seeded) -> None:
    # Векторы ddl/glossary/few-shot orders совпадают с запросом → топ выдачи
    docs = store.search(V_ORDERS, ctx=CTX_ANALYST, top_k=3)
    assert {DocKind.GLOSSARY, DocKind.FEW_SHOT, DocKind.DDL} <= {d.doc_kind for d in docs}
    refs = _refs(docs)
    assert f"{_REF_PREFIX}glossary-orders" in refs
    assert f"{_REF_PREFIX}few-shot-orders" in refs


def test_denied_table_never_surfaces_even_with_exact_match(store, seeded) -> None:
    # Запрос = вектор документа bonuses: без фильтра это cosine distance 0 (топ-1).
    # u_analyst без прав на bonuses → документ не в выдаче вообще (NFR-4 ACL-leak = 0)
    analyst_docs = store.search(V_BONUSES, ctx=CTX_ANALYST, top_k=50)
    assert f"{_REF_PREFIX}ddl-bonuses" not in _refs(analyst_docs)
    assert "bonuses" not in {t for d in analyst_docs for t in d.referenced_tables}
    # Позитивный контроль: u_hr с правами на bonuses видит тот же документ
    hr_docs = store.search(V_BONUSES, ctx=CTX_HR, top_k=5)
    assert f"{_REF_PREFIX}ddl-bonuses" in _refs(hr_docs)


def test_cross_table_doc_hidden_without_rights_on_referenced_table(store, seeded) -> None:
    # Регресс ревью S06 C1: few-shot-cross привязан к разрешённой orders и упоминает
    # bonuses — для u_analyst скрыт (до фикса возвращался: фильтр смотрел только якорь)
    analyst_docs = store.search(V_ORDERS, ctx=CTX_ANALYST, top_k=50)
    assert f"{_REF_PREFIX}few-shot-cross" not in _refs(analyst_docs)
    # Позитивный контроль: временный пользователь с правами на ОБЕ таблицы видит документ
    service_dsn = load_settings().postgres_dsn.get_secret_value()
    with psycopg.connect(service_dsn) as conn:
        conn.execute(
            "DELETE FROM agent_user_roles WHERE user_id = 'it-cross-user';"
            "DELETE FROM agent_permissions WHERE role = 'it_cross_viewer';"
            "INSERT INTO agent_permissions (role, table_name, columns, updated_by) VALUES"
            " ('it_cross_viewer', 'orders', '*', 'it'), ('it_cross_viewer', 'bonuses', '*', 'it');"
            "INSERT INTO agent_user_roles (user_id, role)"
            " VALUES ('it-cross-user', 'it_cross_viewer');"
        )
    try:
        ctx_cross = RunContext(trace_id="it-vector", user_id="it-cross-user")
        cross_docs = store.search(V_ORDERS, ctx=ctx_cross, top_k=50)
        assert f"{_REF_PREFIX}few-shot-cross" in _refs(cross_docs)
    finally:
        with psycopg.connect(service_dsn) as conn:
            conn.execute(
                "DELETE FROM agent_user_roles WHERE user_id = 'it-cross-user';"
                "DELETE FROM agent_permissions WHERE role = 'it_cross_viewer';"
            )


def test_unknown_user_gets_only_common_documents(store, seeded) -> None:
    # Deny-by-default: нет строки в agent_user_roles → только referenced_tables = '{}'
    docs = store.search(V_COMMON, ctx=CTX_UNKNOWN, top_k=50)
    assert docs, "общие документы видны и без ролей"
    assert all(not d.referenced_tables for d in docs)
    assert f"{_REF_PREFIX}glossary-common" in _refs(docs)


def test_migration_backfill_restores_list_from_content(store) -> None:
    """Регресс ревью-2 I2: строка в промежуточном состоянии ('{}' при упоминании
    таблиц в контенте) после backfill миграции 0007 получает полный список —
    инвариант прав держится сразу после make infra-migrate, без ручного шага."""
    import importlib.util
    from pathlib import Path as _Path

    mig_path = (
        _Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "20261120_0007_knowledge_refs_backfill.py"
    )
    spec = importlib.util.spec_from_file_location("mig0007", mig_path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    service_dsn = load_settings().postgres_dsn.get_secret_value()
    with psycopg.connect(service_dsn) as conn:
        conn.execute(
            f"DELETE FROM agent_knowledge WHERE source_ref = '{_REF_PREFIX}backfill-test';"
        )
        conn.execute(
            "INSERT INTO agent_knowledge (doc_kind, table_name, referenced_tables, content,"
            " embedding, model_version, source_ref)"
            " VALUES ('glossary', 'bonuses', '{}', '[термин: backfill-test] премии"
            " employees и bonuses: b.amount', '[" + "0," * 1023 + "0]'::vector, 'synthetic',"
            " %s)",
            (f"{_REF_PREFIX}backfill-test",),
        )
    try:
        with psycopg.connect(service_dsn) as conn:
            conn.execute(mig._BACKFILL_SQL)
            row = conn.execute(
                "SELECT referenced_tables FROM agent_knowledge WHERE source_ref = %s",
                (f"{_REF_PREFIX}backfill-test",),
            ).fetchone()
        assert set(row[0]) == {"employees", "bonuses"}  # восстановлен из контента
        # Вектор ортогонален V_ORDERS/V_BONUSES-кластерам — релевантность не важна:
        # документ скрыт от u_analyst (ни employees, ни bonuses ему недоступны)
        docs = store.search(V_ORDERS, ctx=CTX_ANALYST, top_k=50)
        assert f"{_REF_PREFIX}backfill-test" not in _refs(docs)
    finally:
        with psycopg.connect(service_dsn) as conn:
            conn.execute(
                f"DELETE FROM agent_knowledge WHERE source_ref = '{_REF_PREFIX}backfill-test';"
            )


def test_real_corpus_leaks_no_tables_outside_rights(store) -> None:
    """Инвариант C1 на живом корпусе (make index-knowledge): всё, что видит
    u_analyst, ссылается только на его таблицы (orders/order_items/products)."""
    settings = load_settings()
    with psycopg.connect(settings.postgres_dsn.get_secret_value()) as conn:
        row = conn.execute(
            "SELECT count(*) FROM agent_knowledge WHERE source_ref NOT LIKE %s",
            (f"{_REF_PREFIX}%",),
        ).fetchone()
    if row[0] == 0:
        pytest.skip("корпус не проиндексирован — выполните make index-knowledge")
    docs = store.search(V_ORDERS, ctx=CTX_ANALYST, top_k=100)  # > размера корпуса
    real = [d for d in docs if not d.source_ref.startswith(_REF_PREFIX)]
    assert real, "живой корпус должен быть виден разрешённому пользователю"
    allowed = {"orders", "order_items", "products"}
    mentioned_re = re.compile(
        r"(customers|campaigns|products|orders|order_items|employees|bonuses)"
    )
    for d in real:
        assert set(d.referenced_tables) <= allowed, (
            f"{d.source_ref}: referenced_tables {d.referenced_tables} выходят за права u_analyst"
        )
        # content-scan (ревью-2): упомянутые в контенте таблицы ⊆ объявленным —
        # под-declaration не пронесёт имя недоступной таблицы в контекст
        mentioned = set(mentioned_re.findall(d.content))
        assert mentioned <= set(d.referenced_tables), (
            f"{d.source_ref}: контент упоминает {mentioned}, объявлено {d.referenced_tables}"
        )
        assert mentioned <= allowed, f"{d.source_ref}: контент упоминает таблицы вне прав"
