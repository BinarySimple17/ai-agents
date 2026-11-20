"""Integration-тесты векторной памяти (feat-006, S06) — команда верификации фичи.

behavior feat-006: поиск по эмбеддингу возвращает только документы разрешённых
таблиц; в выдаче есть глоссарий и few-shot релевантных таблиц; недоступные
таблицы не попадают в контекст даже при близком совпадении эмбеддинга
(NFR-4 ACL-leak = 0). Фильтр прав — джойн в SQL поиска (ADR-005 deny-by-default,
не пост-фильтрация); чтение — роль agent_planner, запись — сервисный DSN
(ruling W1 ARCH-GATE S06). Скипаются без инфры с понятной причиной (conftest).

Эмбеддинги в тестах — синтетические (S00 правило 7: реальный шлюз в тестах не
вызывается): ортонормированные 1024-мерные векторы образуют «кластеры» таблиц
(cosine 0 между кластерами) — релевантность определяется вектором запроса, а
эффект фильтра прав — чисто табличным джойном. Сид — через идемпотентный upsert
адаптера (попутно покрывает upsert на живой БД); очистка — по префиксу source_ref.
"""

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
    def doc(kind: DocKind, table: str | None, content: str, ref: str) -> KnowledgeDocument:
        return KnowledgeDocument(
            doc_kind=kind,
            table_name=table,
            content=content,
            model_version=_MODEL,
            source_ref=f"{_REF_PREFIX}{ref}",  # префикс — ключ очистки тестового сида
        )

    docs = [
        doc(
            DocKind.DDL,
            "orders",
            "[таблица: orders] DDL: orders(order_id, customer_id, total)",
            "ddl-orders",
        ),
        doc(
            DocKind.DDL,
            "bonuses",
            "[таблица: bonuses] DDL: bonuses(bonus_id, employee_id, amount)",
            "ddl-bonuses",
        ),
        doc(
            DocKind.GLOSSARY,
            "orders",
            "[термин: выручка-test] выручка = SUM(orders.total)",
            "glossary-orders",
        ),
        doc(
            DocKind.FEW_SHOT,
            "orders",
            "[few-shot: test] Сколько заказов? → SELECT COUNT(*) FROM orders",
            "few-shot-orders",
        ),
        doc(
            DocKind.GLOSSARY,
            None,
            "[термин: общий-test] термин без привязки к таблице",
            "glossary-common",
        ),
    ]
    vectors = [V_ORDERS, V_BONUSES, V_ORDERS, V_ORDERS, V_COMMON]
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
    tables = {d.table_name for d in docs}
    assert "bonuses" not in tables  # недоступная таблица — не существует в мире агента
    assert "employees" not in tables  # и вторая HR-таблица тоже
    assert f"{_REF_PREFIX}ddl-orders" in {d.source_ref for d in docs}  # разрешённая — в выдаче


def test_glossary_and_few_shot_surface_for_relevant_tables(store, seeded) -> None:
    # Векторы glossary-orders/few-shot-orders совпадают с запросом → топ выдачи
    docs = store.search(V_ORDERS, ctx=CTX_ANALYST, top_k=3)
    kinds = {d.doc_kind for d in docs[:3]}
    assert {DocKind.GLOSSARY, DocKind.FEW_SHOT, DocKind.DDL} == kinds
    refs = {d.source_ref for d in docs}
    assert f"{_REF_PREFIX}glossary-orders" in refs
    assert f"{_REF_PREFIX}few-shot-orders" in refs


def test_denied_table_never_surfaces_even_with_exact_match(store, seeded) -> None:
    # Запрос = вектор документа bonuses: без фильтра это cosine distance 0 (топ-1).
    # u_analyst без прав на bonuses → документ не в выдаче вообще (NFR-4 ACL-leak = 0)
    analyst_docs = store.search(V_BONUSES, ctx=CTX_ANALYST, top_k=50)
    assert f"{_REF_PREFIX}ddl-bonuses" not in {d.source_ref for d in analyst_docs}
    assert "bonuses" not in {d.table_name for d in analyst_docs}
    # Позитивный контроль: u_hr с правами на bonuses видит тот же документ
    hr_docs = store.search(V_BONUSES, ctx=CTX_HR, top_k=5)
    assert f"{_REF_PREFIX}ddl-bonuses" in {d.source_ref for d in hr_docs}


def test_unknown_user_gets_only_common_documents(store, seeded) -> None:
    # Deny-by-default: нет строки в agent_user_roles → только table_name IS NULL
    docs = store.search(V_COMMON, ctx=CTX_UNKNOWN, top_k=50)
    assert docs, "общие документы видны и без ролей"
    assert all(d.table_name is None for d in docs)
    assert f"{_REF_PREFIX}glossary-common" in {d.source_ref for d in docs}
