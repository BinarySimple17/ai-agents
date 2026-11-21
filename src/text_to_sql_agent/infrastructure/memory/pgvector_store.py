"""Адаптер `VectorStore` на pgvector (psycopg 3) — гибридная память знаний (S26, feat-021).

Каналы (ruling W1 ARCH-GATE S06): поиск — DSN роли `agent_planner` (c4-l2 v1.8:
«пул agent_planner: retrieval, настройки, EXPLAIN»; гранты — миграции 0002/0005);
запись (upsert) — только офлайн-индексация `make index-knowledge` под сервисным
DSN (`write_dsn`; тот же уровень доверия, что миграции/сиды) — runtime-роли
пишут в память знаний не могут (ревью S06 Important-2: дефолтный контейнер
собирается без write_dsn, writer — явный флаг).

Поиск — гибрид dense+FTS (канон D-026 с дополнениями, BL-007; S26 Task 3):
(A) dense — cosine `<=>` по HNSW-индексу; (B) ключевая нога — PostgreSQL FTS,
ts_rank по generated-колонке content_tsv (миграция 0010) с GIN, tsquery по
нормализованному тексту запроса (plainto_tsquery('russian', …); латинские
имена таблиц/колонок совпадают точным термом — ровно слабое место dense,
ради которого нога добавлялась). Слияние — взвешенный RRF:

    score = α_dense/(RRF_K + rank_dense) + (1−α_dense)/(RRF_K + rank_fts)

α_dense = 0.9 — число-якорь D-026 доп. 1(1): невзвешенный RRF измеримо
проигрывает чистому dense (BEIR ArguAna nDCG@10 0.560 vs 0.642 — слабая нога
диктует порядок), их α*=0.90 дал гибрид ≥ dense; 0.9 — старт без тюнинга,
перекалибровка по hit@5-базлайну (bench_retrieval). Каждая нога берёт
LIMIT MAX_LEG_DEPTH, срез top_k — ПОСЛЕ слияния (ruling N3 ARCH-GATE S26:
RRF-ранги считаются по глубине ноги, короткие ноги ломают ранжирование).

Фильтр прав — deny-by-default (ADR-005 «граница знаний = граница прав», NFR-4
ACL-leak = 0): **джойн в самом SQL ОБЕИХ ног ДО слияния** (D-026/Ловушки-2:
слияние с нелегальными документами + пост-фильтр = дыра), не пост-фильтрация;
инвариант — по всем `referenced_tables` документа (ревью S06 Critical-1).
Недоступный документ не участвует в поиске вообще («не существует в мире
агента»). Смена эмбеддера — фильтр по model_version до пересборки (BL-009 M3,
ruling Task 5 S26): строки чужого векторного пространства не смешиваются с
текущим (expected_model_version; None — фильтра нет, sythetic-корпуса тестов).

Запись — replace-семантика полной коллекции (BL-009 M3, ruling N1 ARCH-GATE
S26): upsert всех переданных + транзакционное удаление устаревших source_ref
в ТОЙ ЖЕ транзакции. Удаление скоупится неймспейсами (префикс source_ref до
':') переданной коллекции: переиндексация источников куратора чистит их
устаревшие ref'ы, а живой корпус (кураторские + шум) и сиды integration-тестов
(it-vector:) в одной таблице не затирают друг друга — полный «стёр всех
чужих» сделал бы тестовый сид оружием массовой очистки. Контент нормализуется
при записи (снятие U+0301, ё→е — text_normalizer, D-026 доп. 1(2)): generated-
колонка content_tsv берёт content как есть (конфиг литералом, immutable —
Ловушки-3), нормализация возможна только до INSERT.

Соединение — на вызов (MVP без пула, как у postgres_executor); таймаут запроса —
`SET LOCAL statement_timeout` канонного значения канала исполнения. Сбой
хранилища — типизированный `VectorStoreError` (прецедент SQLExecutionError),
не сырой psycopg.Error.
"""

import psycopg
from psycopg.rows import dict_row

from text_to_sql_agent.application.ports.sql_executor import (
    CONNECT_TIMEOUT_S,
    STATEMENT_TIMEOUT_S,
)
from text_to_sql_agent.application.ports.vector_store import (
    EMBEDDING_DIM,
    KnowledgeDocument,
    VectorStoreError,
)
from text_to_sql_agent.domain.entities import RunContext
from text_to_sql_agent.infrastructure.memory.text_normalizer import normalize_text

_ERROR_MESSAGE_MAX_CHARS = 300  # сообщения стыка — не простыня psycopg-хинтов

# --- Числа-якоря взвешенного RRF (D-026 доп. 1(1); Ловушки-6: именованные, не в SQL) ---
# α_dense: вес dense-ноги. 0.9 — старт без тюнинга: невзвешенный RRF (0.5) проигрывает
# чистому dense (BEIR: ArguAna 0.560 vs 0.642, FiQA 0.338 vs 0.423 — документ слабой
# ноги на ранге 1 получает 1/61 и бьёт dense-ранг 2); α*=0.90 дал гибрид ≥ dense на
# обоих корпусах родственного проекта. Перекалибровка — по hit@5-базлайну bench_retrieval.
RRF_ALPHA_DENSE = 0.9
# RRF_K: каноническая константа Reciprocal Rank Fusion (Cormack et al., 2009).
RRF_K = 60
# Глубина ноги: сколько строк берёт каждая нога до слияния. На ~350 документах не
# срабатывает (корпус меньше), страхует рост: длинный запрос одним термом без капа
# затаскивает тысячи документов в ранжирование (D-026 доп. 1(1)); ≥ top_k (8, S24).
MAX_LEG_DEPTH = 50

# Права — по всем таблицам документа (NFR-4, ревью S06 Critical-1): NOT EXISTS
# (... NOT EXISTS ...) = «нет ни одной таблицы без прав». Внутренний EXISTS — джойн
# с настройками прав (agent_user_roles → agent_permissions), deny-by-default для
# неизвестных user_id. ОБЩИЙ SQL-фрагмент ОБЕИХ ног (Task 3: вынести — DRY):
# слияние с нелегальным документом + пост-фильтр = дыра, фильтр ДО слияния.
_RIGHTS_FILTER_SQL = """(
       referenced_tables = '{}'
    OR NOT EXISTS (
        SELECT 1
        FROM unnest(referenced_tables) AS t
        WHERE NOT EXISTS (
            SELECT 1
            FROM agent_user_roles ur
            JOIN agent_permissions p ON p.role = ur.role
            WHERE ur.user_id = %s AND p.table_name = t
        )
    )
)"""

# Фильтр model_version (BL-009 M3): строки чужого векторного пространства
# (смена эмбеддера до пересборки) не смешиваются с текущим. Встраивается ВНУТРИ
# шаблона ноги (до ORDER BY/LIMIT), не доклейкой
_MODEL_FILTER_SQL = "\n    AND model_version = %s"

# Нога A — dense (pgvector cosine); LIMIT MAX_LEG_DEPTH — ранги RRF по глубине ноги
_DENSE_LEG_SQL_TEMPLATE = """
SELECT id, doc_kind, table_name, referenced_tables, content, model_version, source_ref,
       1 - (embedding <=> %s::vector) AS leg_score
FROM agent_knowledge
WHERE {rights}{model}
ORDER BY embedding <=> %s::vector, id
LIMIT {depth}
"""

# Нога B — FTS (ts_rank по content_tsv, миграция 0010): tsquery считается один раз
# в CTE (SELECT и WHERE используют один и тот же лексический разбор); текст запроса
# нормализуется ДО plainto_tsquery (text_normalizer: U+0301 режет термы пополам)
_FTS_LEG_SQL_TEMPLATE = """
WITH q AS (SELECT plainto_tsquery('russian', %s) AS tsq)
SELECT k.id, k.doc_kind, k.table_name, k.referenced_tables, k.content, k.model_version,
       k.source_ref, ts_rank(k.content_tsv, q.tsq) AS leg_score
FROM agent_knowledge k, q
WHERE k.content_tsv @@ q.tsq
  AND {rights}{model}
ORDER BY leg_score DESC, k.id
LIMIT {depth}
"""

# Замена устаревших source_ref (BL-009 M3, ruling N1): только неймспейсы
# (префикс source_ref до ':') переиндексируемой коллекции и только ref'ы,
# отсутствующие в ней — см. докстринг модуля
_DELETE_STALE_SQL = """
DELETE FROM agent_knowledge
WHERE split_part(source_ref, ':', 1) = ANY(%s)
  AND source_ref <> ALL(%s)
"""

# Replace-идемпотентность — по source_ref (unique index миграции 0005): повторная
# индексация обновляет содержимое, вектор и список таблиц, не дублирует документ.
_UPSERT_SQL = """
INSERT INTO agent_knowledge
    (doc_kind, table_name, referenced_tables, content, embedding, model_version, source_ref)
VALUES (%s, %s, %s, %s, %s::vector, %s, %s)
ON CONFLICT (source_ref) DO UPDATE
SET doc_kind = EXCLUDED.doc_kind,
    table_name = EXCLUDED.table_name,
    referenced_tables = EXCLUDED.referenced_tables,
    content = EXCLUDED.content,
    embedding = EXCLUDED.embedding,
    model_version = EXCLUDED.model_version,
    updated_at = now()
"""


def _dense_leg_sql(*, model_filter: bool) -> str:
    return _DENSE_LEG_SQL_TEMPLATE.format(
        rights=_RIGHTS_FILTER_SQL,
        model=_MODEL_FILTER_SQL if model_filter else "",
        depth=MAX_LEG_DEPTH,
    )


def _fts_leg_sql(*, model_filter: bool) -> str:
    # Права-фрагмент квалифицируется алиасом k (запрос FTS-ноги алиасит
    # agent_knowledge): resolve без двусмысленностей с agent_permissions.table_name
    rights = _RIGHTS_FILTER_SQL.replace("referenced_tables", "k.referenced_tables")
    model = _MODEL_FILTER_SQL.replace("model_version", "k.model_version") if model_filter else ""
    return _FTS_LEG_SQL_TEMPLATE.format(rights=rights, model=model, depth=MAX_LEG_DEPTH)


def _rrf_merge(
    dense_docs: list[KnowledgeDocument],
    fts_docs: list[KnowledgeDocument],
    *,
    alpha_dense: float,
) -> list[KnowledgeDocument]:
    """Взвешенный RRF (D-026 доп. 1(1)) — чистая функция, тестируется без БД.

    Ранги 1-based по каждой ноге; нога, не нашедшая документ, не вносит вклад
    (документ «найден только одной ногой»). Итоговый score — RRF-оценка, она
    же пишется в KnowledgeDocument.score. При равных score — детерминированный
    tie-break по source_ref (ранжирование не зависит от порядка ног).
    """
    scores: dict[str, float] = {}
    by_ref: dict[str, KnowledgeDocument] = {}
    for rank, doc in enumerate(dense_docs, start=1):
        scores[doc.source_ref] = scores.get(doc.source_ref, 0.0) + alpha_dense / (RRF_K + rank)
        by_ref.setdefault(doc.source_ref, doc)
    for rank, doc in enumerate(fts_docs, start=1):
        weight = 1.0 - alpha_dense
        scores[doc.source_ref] = scores.get(doc.source_ref, 0.0) + weight / (RRF_K + rank)
        by_ref.setdefault(doc.source_ref, doc)
    merged = [doc.model_copy(update={"score": scores[doc.source_ref]}) for doc in by_ref.values()]
    merged.sort(key=lambda doc: (-(doc.score or 0.0), doc.source_ref))
    return merged


def _to_pgvector(vector: list[float]) -> str:
    """Вектор → литерал pgvector '[0.1,-0.2]'; repr(float) сохраняет точность."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _check_dim(vector: list[float], *, what: str) -> None:
    if len(vector) != EMBEDDING_DIM:
        raise ValueError(
            f"{what}: размерность {len(vector)} ≠ EMBEDDING_DIM={EMBEDDING_DIM} — "
            "смена эмбеддера = переиндексация (model-card §1)"
        )


def _shorten(message: str) -> str:
    message = " ".join(message.split())
    if len(message) <= _ERROR_MESSAGE_MAX_CHARS:
        return message
    return message[: _ERROR_MESSAGE_MAX_CHARS - 1] + "…"


def _doc_from_row(row: dict) -> KnowledgeDocument:
    return KnowledgeDocument(
        doc_kind=row["doc_kind"],
        table_name=row["table_name"],
        referenced_tables=list(row["referenced_tables"]),
        content=row["content"],
        model_version=row["model_version"],
        source_ref=row["source_ref"],
        score=float(row["leg_score"]),
    )


class PgVectorStore:
    """Реализация `VectorStore`: гибридный поиск (dense+FTS, взвешенный RRF)
    с фильтром прав в обеих ногах + replace-идемпотентный upsert."""

    def __init__(
        self,
        search_dsn: str,
        *,
        write_dsn: str | None = None,
        expected_model_version: str | None = None,
        rrf_alpha_dense: float = RRF_ALPHA_DENSE,
    ) -> None:
        if not 0.0 <= rrf_alpha_dense <= 1.0:
            raise ValueError(
                f"rrf_alpha_dense={rrf_alpha_dense} вне [0, 1] — вес dense-ноги RRF "
                "(0 — только FTS, 1 — только dense; канон D-026: 0.9)"
            )
        self._search_dsn = search_dsn
        self._write_dsn = write_dsn
        self._expected_model_version = expected_model_version
        self._rrf_alpha_dense = rrf_alpha_dense

    def search(
        self,
        query: str,
        query_embedding: list[float],
        *,
        ctx: RunContext,
        top_k: int,
    ) -> list[KnowledgeDocument]:
        _check_dim(query_embedding, what="вектор запроса")
        if top_k <= 0:
            raise ValueError(f"top_k должен быть положительным, получен {top_k}")
        if not query.strip():
            raise ValueError(
                "query пуст — гибридному поиску нужен текст запроса FTS-ноги "
                "(сигнатура S26: search(query, query_embedding, *, ctx, top_k))"
            )
        normalized_query = normalize_text(query)
        vector_literal = _to_pgvector(query_embedding)
        model_filter = self._expected_model_version is not None
        model_param = [self._expected_model_version] if model_filter else []
        try:
            with psycopg.connect(
                self._search_dsn,
                row_factory=dict_row,
                connect_timeout=CONNECT_TIMEOUT_S,
            ) as conn:
                conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_S}s'")
                # Обе ноги — на одном соединении: один снапшот на слияние
                dense_params = [vector_literal, ctx.user_id, *model_param, vector_literal]
                dense_rows = conn.execute(
                    _dense_leg_sql(model_filter=model_filter), dense_params, prepare=True
                ).fetchall()
                fts_params = [normalized_query, ctx.user_id, *model_param]
                fts_rows = conn.execute(
                    _fts_leg_sql(model_filter=model_filter), fts_params, prepare=True
                ).fetchall()
        except psycopg.Error as exc:
            raise VectorStoreError(f"векторная память недоступна: {_shorten(str(exc))}") from exc
        dense_docs = [_doc_from_row(row) for row in dense_rows]
        fts_docs = [_doc_from_row(row) for row in fts_rows]
        merged = _rrf_merge(dense_docs, fts_docs, alpha_dense=self._rrf_alpha_dense)
        return merged[:top_k]

    def upsert(self, documents: list[KnowledgeDocument], embeddings: list[list[float]]) -> int:
        if len(documents) != len(embeddings):
            raise ValueError(
                f"documents ({len(documents)}) и embeddings ({len(embeddings)}) — "
                "параллельные списки одной длины"
            )
        for i, vector in enumerate(embeddings):
            _check_dim(vector, what=f"embeddings[{i}]")
        if not documents:
            raise ValueError(
                "upsert пустой коллекции при replace-семантике стёр бы неймспейсы — "
                "передавайте полную коллекцию (ruling N1 ARCH-GATE S26)"
            )
        if self._write_dsn is None:
            raise RuntimeError(
                "upsert требует write_dsn: запись — только офлайн-индексация "
                "(make index-knowledge), runtime-роли в память знаний не пишут (ruling W1)"
            )
        params = [
            (
                doc.doc_kind.value,
                doc.table_name,
                list(doc.referenced_tables),
                # Нормализация ДО записи: generated-колонка content_tsv берёт
                # content как есть (D-026 доп. 1(2), Ловушки-3)
                normalize_text(doc.content),
                _to_pgvector(vector),
                doc.model_version,
                doc.source_ref,
            )
            for doc, vector in zip(documents, embeddings, strict=True)
        ]
        source_refs = [doc.source_ref for doc in documents]
        namespaces = sorted({ref.split(":", 1)[0] for ref in source_refs})
        try:
            with psycopg.connect(
                self._write_dsn,
                row_factory=dict_row,
                connect_timeout=CONNECT_TIMEOUT_S,
            ) as conn:
                conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_S}s'")
                with conn.cursor() as cursor:
                    cursor.executemany(_UPSERT_SQL, params)
                    # Устаревшие ref'ы переиндексируемых неймспейсов — той же
                    # транзакцией (BL-009 M3); psycopg 3: одним conn — одна
                    # транзакция, COMMIT при выходе из with
                    cursor.execute(_DELETE_STALE_SQL, (namespaces, source_refs))
        except psycopg.Error as exc:
            raise VectorStoreError(
                f"запись в векторную память не удалась: {_shorten(str(exc))}"
            ) from exc
        return len(documents)
