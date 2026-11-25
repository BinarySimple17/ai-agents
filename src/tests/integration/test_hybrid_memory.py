"""Integration-тесты гибридной памяти (feat-021, S26 Task 7) — верификация фичи.

behavior feat-021: (a) ACL-leak = 0 в ОБЕИХ ногах — точный текстовый матч по
недоступной таблице не возвращается; (b) дистракторы ранжируются вместе с
реальными, но не попадают в сборку промпта; (c) гибрид ≥ dense на замерном
множестве (фиксированные вопросы; реальный замер и baseline — make
bench-retrieval, layer-3, model-card §1); (d) переиндексация транзакционно
чистит устаревшие source_ref; (e) нормализация U+0301/ё→е на upsert и запросе.

Эмбеддинги синтетические (S00 правило 7 — реальный шлюз в тестах не вызывается):
ортонормированные 1024-мерные кластеры, как в test_vector_memory. Сид — своим
неймспейсом `it-hybrid:` (replace-семантика upsert удаляет устаревшие ref'ы
ТОЛЬКО неймспейсов переданной коллекции — живой корпус тест не затирает).
Скипаются без инфры с понятной причиной (conftest).
"""

import psycopg
import pytest

from apps.cli import format_retrieval_block
from text_to_sql_agent.application.ports.vector_store import DocKind, KnowledgeDocument
from text_to_sql_agent.config.settings import load_settings
from text_to_sql_agent.domain.entities import RunContext
from text_to_sql_agent.infrastructure.memory.pgvector_store import PgVectorStore

_REF_PREFIX = "it-hybrid:"
_DIM = 1024
_MODEL = "synthetic-test-embedder"

CTX_ANALYST = RunContext(trace_id="it-hybrid", user_id="u_analyst")
CTX_HR = RunContext(trace_id="it-hybrid", user_id="u_hr")
CTX_LOGIST = RunContext(trace_id="it-hybrid", user_id="u_logist")  # campaigns в правах


def _unit(i: int) -> list[float]:
    return [1.0 if j == i else 0.0 for j in range(_DIM)]


V_ORDERS = _unit(0)
V_BONUSES = _unit(1)  # кластер, недоступный u_analyst
V_CAMPAIGNS = _unit(2)


def _doc(
    kind: DocKind,
    tables: list[str],
    content: str,
    ref: str,
    vector: list[float],
) -> tuple[KnowledgeDocument, list[float]]:
    doc = KnowledgeDocument(
        doc_kind=kind,
        table_name=tables[0] if tables else None,
        referenced_tables=tables,
        content=content,
        model_version=_MODEL,
        source_ref=f"{_REF_PREFIX}{ref}",
    )
    return doc, vector


def _seed_docs() -> tuple[list[KnowledgeDocument], list[list[float]]]:
    """Синтетический корпус: знание + дистракторы, делящие признак с целью."""
    docs, vectors = zip(
        # знание — контент ко-лексифицирован (plainto_tsquery — AND-семантика:
        # оба терма вопроса в тексте), как в живом кураторском корпусе
        _doc(
            DocKind.GLOSSARY,
            ["orders"],
            "[термин: выручка-test] выручка по заказам = SUM(orders.total)",
            "glossary-vyruchka",
            V_ORDERS,
        ),
        _doc(
            DocKind.DDL,
            ["orders"],
            "[таблица: orders] DDL: orders(order_id, customer_id, total)",
            "ddl-orders",
            V_ORDERS,
        ),
        _doc(
            DocKind.FEW_SHOT,
            ["orders"],
            "[few-shot: test] позиции заказа quantity: unit_price",
            "few-shot-pozicii",
            V_ORDERS,
        ),
        _doc(
            DocKind.FEW_SHOT,
            ["campaigns"],
            "[few-shot: кампании-test] выручка по кампаниям: JOIN campaigns",
            "few-shot-kampanii",
            V_CAMPAIGNS,
        ),
        # знание HR-таблиц (u_analyst недоступно)
        _doc(
            DocKind.GLOSSARY,
            ["bonuses"],
            "[термин: премии-test] премии сотрудников bonuses.amount",
            "glossary-premii",
            V_BONUSES,
        ),
        # дистракторы: ДЕЛЯТ признак с целью (та же таблица/лексика), doc_kind noise
        _doc(
            DocKind.NOISE,
            ["orders"],
            "[шум: orders] заметка про выручку и заказы, черновик регламента #1",
            "noise-001",
            V_ORDERS,
        ),
        _doc(
            DocKind.NOISE,
            ["orders", "bonuses"],
            "[шум: orders, bonuses] сверка премий и заказов, черновик #2",
            "noise-002",
            V_BONUSES,
        ),
        _doc(
            DocKind.NOISE,
            ["campaigns"],
            "[шум: campaigns] протокол по рекламным кампаниям #3",
            "noise-003",
            V_CAMPAIGNS,
        ),
        strict=True,
    )
    return list(docs), list(vectors)


def _cleanup(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("DELETE FROM agent_knowledge WHERE source_ref LIKE %s", (f"{_REF_PREFIX}%",))


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


def test_fts_leg_filters_rights_exact_text_match(store, seeded) -> None:
    """(a) ACL-leak = 0 на FTS-ноге: точный текстовый матч по недоступной
    таблице (bonuses) не возвращается u_analyst — фильтр ДО слияния (NFR-4)."""
    analyst_docs = store.search("премии bonuses amount", V_ORDERS, ctx=CTX_ANALYST, top_k=50)
    assert f"{_REF_PREFIX}glossary-premii" not in _refs(analyst_docs)
    assert f"{_REF_PREFIX}noise-002" not in _refs(analyst_docs)  # шум тоже отфильтрован
    assert "bonuses" not in {t for d in analyst_docs for t in d.referenced_tables}
    # Позитивный контроль: u_hr с правами на bonuses видит тот же текстовый матч
    hr_docs = store.search("премии bonuses amount", V_BONUSES, ctx=CTX_HR, top_k=5)
    assert f"{_REF_PREFIX}glossary-premii" in _refs(hr_docs)


def test_noise_ranks_but_never_enters_prompt(store, seeded) -> None:
    """(b) дистракторы ранжируются вместе с реальными документами (в выдаче
    поиска допустимы), но в retrieval-блок промпта не попадают никогда."""
    docs = store.search("выручка по заказам", V_ORDERS, ctx=CTX_ANALYST, top_k=8)
    noise_in_results = [d for d in docs if d.doc_kind is DocKind.NOISE]
    assert noise_in_results, "дистракторы должны участвовать в ранжировании (D-026)"
    block = format_retrieval_block(docs)
    assert "[шум:" not in block
    for noise in noise_in_results:
        assert noise.source_ref.rsplit(":", 1)[-1] not in block
        assert noise.content not in block
    assert "[термин: выручка-test]" in block  # знание — в блоке


def test_hybrid_hits_at_least_dense_on_fixed_questions(store, seeded) -> None:
    """(c) гибрид ≥ dense на замерном множестве фиксированных вопросов.

    Синтетический корпус (реальный шлюз в тестах не вызывается — S00 правило 7);
    настоящий замер и baseline — make bench-retrieval (layer-3, model-card §1).
    Инвариант D-026 доп. 1(1): взвешенный RRF (α=0.9) не теряет хиты dense.
    """
    settings = load_settings()
    store_dense = PgVectorStore(  # α=1.0: вклад FTS-ноги нулевой — порядок = dense
        settings.agent_planner_dsn,
        rrf_alpha_dense=1.0,
    )
    questions: tuple[tuple[str, list[float], tuple[str, ...], RunContext], ...] = (
        ("выручка по заказам", V_ORDERS, (f"{_REF_PREFIX}glossary-vyruchka",), CTX_ANALYST),
        ("позиции заказа quantity", V_ORDERS, (f"{_REF_PREFIX}few-shot-pozicii",), CTX_ANALYST),
        # campaigns — вне прав u_analyst: вопрос под u_logist (logistics_viewer)
        ("выручка по кампаниям", V_CAMPAIGNS, (f"{_REF_PREFIX}few-shot-kampanii",), CTX_LOGIST),
        ("премии сотрудникам", V_BONUSES, (f"{_REF_PREFIX}glossary-premii",), CTX_HR),
    )
    dense_hits = hybrid_hits = 0
    for question, vector, targets, ctx in questions:
        for mode, store_mode in (("dense", store_dense), ("hybrid", store)):
            docs = store_mode.search(question, vector, ctx=ctx, top_k=8)
            if any(doc.source_ref in targets for doc in docs[:5]):
                if mode == "dense":
                    dense_hits += 1
                else:
                    hybrid_hits += 1
    assert hybrid_hits >= dense_hits, (
        f"гибрид ({hybrid_hits}) потерял хиты dense ({dense_hits}) — инвариант D-026 нарушен"
    )
    assert hybrid_hits == len(questions)  # синтетический замер — все цели достижимы


def test_reindex_deletes_stale_source_refs_transactionally(store) -> None:
    """(d) BL-009 M3: переиндексация после удаления источника чистит устаревшие
    source_ref той же транзакцией (replace-семантика, ruling N1)."""
    service_dsn = load_settings().postgres_dsn.get_secret_value()
    docs, vectors = _seed_docs()
    keep = [docs[0], docs[1]]  # glossary-vyruchka, ddl-orders остаются
    keep_vectors = [vectors[0], vectors[1]]
    try:
        assert store.upsert(docs, vectors) == len(docs)

        def _count(ref: str) -> int:
            with psycopg.connect(service_dsn) as conn:
                row = conn.execute(
                    "SELECT count(*) FROM agent_knowledge WHERE source_ref = %s", (ref,)
                ).fetchone()
            return row[0]

        full_ref = f"{_REF_PREFIX}glossary-premii"
        assert _count(full_ref) == 1
        # Переиндексация урезанной коллекцией: её неймспейс чистится от отсутствующих
        assert store.upsert(keep, keep_vectors) == len(keep)
        assert _count(full_ref) == 0  # устаревший ref удалён транзакцией upsert
        assert _count(f"{_REF_PREFIX}glossary-vyruchka") == 1  # присутствующий жив
    finally:
        _cleanup(service_dsn)


def test_normalization_yo_and_accent_match_plain_document(store, seeded) -> None:
    """(e) BL-007: вопрос с «ё»/акутом U+0301 находит документ без них —
    один хелпер на upsert (контент нормализован ДО записи) и на запросе."""
    # Контент сида «выручка-test» без ё и акута; запрос — с акутом и ё
    accented_query = "Объём вы́ручки по заказа́м"
    docs = store.search(accented_query, V_ORDERS, ctx=CTX_ANALYST, top_k=5)
    assert f"{_REF_PREFIX}glossary-vyruchka" in _refs(docs)
    # Ё в запросе эквивалентна е: документ с «ё» в сида (нормализован при записи).
    # Отдельный неймспейс (m14 ревью S26): replace-upsert удаляет устаревшие ref'ы
    # только СВОИХ неймспейсов — частичный upsert не подрезает сид фикстуры
    yo_ref = "it-hybrid-norm:glossary-yo"
    yo_doc = KnowledgeDocument(
        doc_kind=DocKind.GLOSSARY,
        table_name="orders",
        referenced_tables=["orders"],
        content="[термин: объём-test] объём данных",
        model_version=_MODEL,
        source_ref=yo_ref,
    )
    service_dsn = load_settings().postgres_dsn.get_secret_value()
    try:
        assert store.upsert([yo_doc], [V_ORDERS]) == 1
        with psycopg.connect(service_dsn) as conn:
            stored = conn.execute(
                "SELECT content FROM agent_knowledge WHERE source_ref = %s", (yo_ref,)
            ).fetchone()
        assert "объем" in stored[0] and "ё" not in stored[0]  # нормализован при записи
        assert _count_ref(service_dsn, f"{_REF_PREFIX}glossary-vyruchka") == 1  # сид жив
        docs = store.search("объем данных", V_ORDERS, ctx=CTX_ANALYST, top_k=5)
        assert yo_ref in _refs(docs)
    finally:
        with psycopg.connect(service_dsn) as conn:
            conn.execute("DELETE FROM agent_knowledge WHERE source_ref = %s", (yo_ref,))


def _count_ref(dsn: str, ref: str) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT count(*) FROM agent_knowledge WHERE source_ref = %s", (ref,)
        ).fetchone()
    return row[0]


def test_pure_fts_leg_works_and_filters_rights(store, seeded) -> None:
    """I4a ревью S26: работоспособность И фильтр прав FTS-ноги — доказаны отдельно
    от dense. α_dense=0.0 (чистая FTS): документ с уникальным термом и ортогональным
    вектором находит ТОЛЬКО FTS-нога (dense-вклад нулевой, score > 0 возможен
    только через content_tsv); у пользователя без прав на referenced_tables
    документ отсутствует вовсе (ACL-leak = 0 на FTS-ноге)."""
    settings = load_settings()
    store_fts_only = PgVectorStore(settings.agent_planner_dsn, rrf_alpha_dense=0.0)
    # Целевой документ: уникальный терм «hr_regression_term», вектор ортогонален
    # запросу (V_ORDERS) — dense его не вытащит; таблица bonuses — вне прав u_analyst
    # Собственный неймспейс (new-m6 ревью S26, как m14): replace-upsert теста
    # не подрезает сид фикстуры
    fts_ref = "it-hybrid-fts:glossary-fts-only"
    target = KnowledgeDocument(
        doc_kind=DocKind.GLOSSARY,
        table_name="bonuses",
        referenced_tables=["bonuses"],
        content="[термин: fts-тест] hr_regression_term в bonuses",
        model_version=_MODEL,
        source_ref=fts_ref,
    )
    service_dsn = settings.postgres_dsn.get_secret_value()
    try:
        assert store.upsert([target], [V_BONUSES]) == 1
        # Позитив: u_hr с правами на bonuses — документ найден ЧИСТОЙ FTS-ногой
        hr_docs = store_fts_only.search("hr_regression_term", V_ORDERS, ctx=CTX_HR, top_k=8)
        hits = [d for d in hr_docs if d.source_ref == fts_ref]
        assert hits, "FTS-нога обязана находить точный текстовый матч (α=0)"
        assert hits[0].score > 0, "score > 0 при α=0 возможен только через FTS-ногу"
        # Негатив: u_analyst без прав на bonuses — документа нет ни одной ногой
        analyst_docs = store_fts_only.search(
            "hr_regression_term", V_ORDERS, ctx=CTX_ANALYST, top_k=8
        )
        assert fts_ref not in _refs(analyst_docs)
    finally:
        with psycopg.connect(service_dsn) as conn:
            # Утечка сида (живой CI 2026-11-25): удалялся it-hybrid:glossary-fts-only,
            # а сеялся it-hybrid-fts:... — строка жила вечно и ломала guard живого
            # корпуса у test_vector_memory (свежая БД CI: count > 0 при пустом поиске)
            conn.execute(
                "DELETE FROM agent_knowledge WHERE source_ref = %s", (fts_ref,)
            )


def test_live_corpus_noise_participates_and_never_in_prompt(store) -> None:
    """(b) на живом корпусе (make index-knowledge): шумовые документы ранжируются
    вместе с кураторскими и не попадают в retrieval-блок промпта."""
    settings = load_settings()
    with psycopg.connect(settings.postgres_dsn.get_secret_value()) as conn:
        row = conn.execute(
            "SELECT count(*) FROM agent_knowledge WHERE doc_kind = 'noise'"
        ).fetchone()
    if row[0] == 0:
        pytest.skip("живой корпус без шума — выполните make index-knowledge")
    docs = store.search("выручка по заказам клиентов", V_ORDERS, ctx=CTX_ANALYST, top_k=8)
    assert any(d.doc_kind is DocKind.NOISE for d in docs), (
        "шум живого корпуса должен ранжироваться вместе с кураторскими документами"
    )
    block = format_retrieval_block(docs)
    assert "[шум:" not in block
