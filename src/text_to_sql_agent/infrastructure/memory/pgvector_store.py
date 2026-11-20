"""Адаптер `VectorStore` на pgvector (psycopg 3) — векторная память знаний (S06, feat-006).

Каналы (ruling W1 ARCH-GATE S06): поиск — DSN роли `agent_planner` (c4-l2 v1.8:
«пул agent_planner: retrieval, настройки, EXPLAIN»; гранты — миграции 0002/0005);
запись (upsert) — только офлайн-индексация `make index-knowledge` под сервисным
DSN (`write_dsn`; тот же уровень доверия, что миграции/сиды) — runtime-роли
пишут в память знаний не могут (ревью S06 Important-2: дефолтный контейнер
собирается без write_dsn, writer — явный флаг).

Поиск — dense-only, cosine `<=>` по HNSW-индексу (решение владельца 2026-11-20,
D-026 доп. 2; расширение до гибрида — V3, BL-007: порт абстрагирует способ
поиска). Фильтр прав — deny-by-default (ADR-005 «граница знаний = граница прав»,
NFR-4 ACL-leak = 0): **джойн в самом SQL поиска** (ADR-003 §4), не
пост-фильтрация; инвариант — по всем `referenced_tables` документа (ревью S06
Critical-1): документ с таблицами [orders, campaigns] виден только пользователю
с правами на ОБЕ; пустой список — общий документ. Недоступный документ не
участвует в поиске вообще («не существует в мире агента»).

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

_ERROR_MESSAGE_MAX_CHARS = 300  # сообщения стыка — не простыня psycopg-хинтов

# Права — по всем таблицам документа: NOT EXISTS(... NOT EXISTS ...)) = «нет ни
# одной таблицы без прав». Внутренний EXISTS — джойн с настройками прав
# (agent_user_roles → agent_permissions), deny-by-default для неизвестных user_id.
_SEARCH_SQL = """
SELECT id, doc_kind, table_name, referenced_tables, content, model_version, source_ref,
       1 - (embedding <=> %s::vector) AS score
FROM agent_knowledge
WHERE referenced_tables = '{}'
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
ORDER BY embedding <=> %s::vector
LIMIT %s
"""

# Идемпотентность — по source_ref (unique index миграции 0005): повторная
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


class PgVectorStore:
    """Реализация `VectorStore`: dense-поиск с фильтром прав + идемпотентный upsert."""

    def __init__(self, search_dsn: str, *, write_dsn: str | None = None) -> None:
        self._search_dsn = search_dsn
        self._write_dsn = write_dsn

    def search(
        self, query_embedding: list[float], *, ctx: RunContext, top_k: int
    ) -> list[KnowledgeDocument]:
        _check_dim(query_embedding, what="вектор запроса")
        if top_k <= 0:
            raise ValueError(f"top_k должен быть положительным, получен {top_k}")
        vector_literal = _to_pgvector(query_embedding)
        try:
            with psycopg.connect(
                self._search_dsn,
                row_factory=dict_row,
                connect_timeout=CONNECT_TIMEOUT_S,
            ) as conn:
                conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_S}s'")
                cursor = conn.execute(
                    _SEARCH_SQL,
                    (vector_literal, ctx.user_id, vector_literal, top_k),
                    prepare=True,
                )
                rows = cursor.fetchall()
        except psycopg.Error as exc:
            raise VectorStoreError(f"векторная память недоступна: {_shorten(str(exc))}") from exc
        return [
            KnowledgeDocument(
                doc_kind=row["doc_kind"],
                table_name=row["table_name"],
                referenced_tables=list(row["referenced_tables"]),
                content=row["content"],
                model_version=row["model_version"],
                source_ref=row["source_ref"],
                score=float(row["score"]),
            )
            for row in rows
        ]

    def upsert(self, documents: list[KnowledgeDocument], embeddings: list[list[float]]) -> int:
        if len(documents) != len(embeddings):
            raise ValueError(
                f"documents ({len(documents)}) и embeddings ({len(embeddings)}) — "
                "параллельные списки одной длины"
            )
        for i, vector in enumerate(embeddings):
            _check_dim(vector, what=f"embeddings[{i}]")
        if not documents:
            return 0
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
                doc.content,
                _to_pgvector(vector),
                doc.model_version,
                doc.source_ref,
            )
            for doc, vector in zip(documents, embeddings, strict=True)
        ]
        try:
            with psycopg.connect(
                self._write_dsn,
                row_factory=dict_row,
                connect_timeout=CONNECT_TIMEOUT_S,
            ) as conn:
                with conn.cursor() as cursor:
                    cursor.executemany(_UPSERT_SQL, params)
        except psycopg.Error as exc:
            raise VectorStoreError(
                f"запись в векторную память не удалась: {_shorten(str(exc))}"
            ) from exc
        return len(documents)
