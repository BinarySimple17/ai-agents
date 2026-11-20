"""Миграция 005 (S06): таблица векторной памяти agent_knowledge — feat-006.

Источники (канон):
- состав и поля — docs/diagrams/er-data-model.md (sync в этом же коммите);
- 4 типа документов (doc_kind) — ADR-003 §4, hw-plan §3;
- поиск dense-only, cosine — решение владельца 2026-11-20 (D-026 доп. 2),
  механика качества — веха V3 (BL-007);
- грант SELECT агенту agent_planner — ruling W1 ARCH-GATE S06 (c4-l2 v1.8:
  «пул agent_planner: retrieval»); запись — только офлайн-индексация под
  сервисным DSN (make index-knowledge), runtime-роли пишут не могут.

Идемпотентность индексации — unique index по source_ref: INSERT … ON CONFLICT
(source_ref) DO UPDATE (повторная индексация обновляет, не дублирует).
Размерность embedding = 1024 (bge-m3, model-card §1): смена эмбеддера =
переиндексация (EMBEDDING_DIM в порту vector_store).
"""

from alembic import op

revision = "s06_0005_vector_memory"
down_revision = "s13_0004_rls_sensitive_tables"
branch_labels = None
depends_on = None

SCHEMA_SQL = r"""
-- === Векторная память знаний (feat-006, ADR-003 §4) ===
CREATE TABLE agent_knowledge (
    id            integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_kind      text NOT NULL CHECK (doc_kind IN ('ddl', 'relationship', 'glossary', 'few_shot')),
    table_name    text,                     -- null = общий документ (не привязан к таблице)
    content       text NOT NULL,            -- чанк с префиксом-заголовком («[таблица: orders] …», ACCURACY §6.2)
    embedding     vector(1024) NOT NULL,    -- размерность bge-m3; смена эмбеддера = переиндексация
    model_version text NOT NULL,            -- каким эмбеддером построен вектор (model-card)
    source_ref    text NOT NULL,            -- ключ идемпотентного upsert (файл:идентификатор)
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX uq_agent_knowledge_source_ref ON agent_knowledge (source_ref);

-- HNSW по cosine (pgvector 0.8.0) — dense-поиск ANN (параметры по умолчанию:
-- корпус ~30 документов, тюнинг m/ef_all не оправдан)
CREATE INDEX ix_agent_knowledge_embedding_hnsw
    ON agent_knowledge USING hnsw (embedding vector_cosine_ops);

-- Грант retrieval-канала (ruling W1 ARCH-GATE S06): пул agent_planner (c4-l2 v1.8);
-- гранты на права-таблицы (agent_user_roles/agent_permissions) — из миграции 0002,
-- джойн прав работает под этой ролью. Запись — только офлайн-индексация (сервисный DSN).
GRANT SELECT ON agent_knowledge TO agent_planner;
"""

DROP_SQL = r"""
DROP TABLE agent_knowledge;
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    op.execute(DROP_SQL)
