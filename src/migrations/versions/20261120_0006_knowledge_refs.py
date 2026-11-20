"""Миграция 006 (S06, fix-волна ревью Critical-1): referenced_tables в agent_knowledge.

Инвариант ACL-leak = 0 (NFR-4, ADR-005) — по ВСЕМ таблицам, упомянутым в
документе, а не только по таблице-якорю: кураторский few-shot «выручка по
кампаниям» ссылается на orders И campaigns — пользователь без прав на campaigns
документ не видит. Фильтр прав в pgvector_store.search переключён на список.

Схема: `referenced_tables text[] NOT NULL DEFAULT '{}'` — пусто = общий документ.
Заполняет индексатор (knowledge_sources): DDL — сама таблица; relationship —
left+right; глоссарий/few-shot — явные метаданные заголовка раздела («таблицы
A, B»), plus индексаторский guard «содержимое не упоминает необъявленные
таблицы» (вторая линия инварианта).

Backfill здесь — только DDL (однозначно из table_name); relationship/glossary/
few_shot остаются '{}' ДО повторной индексации (в этот промежуток их видят все —
уровень уже принятого dev-контура со статическим DDL, ruling ARCH-GATE S06 W2).
После миграции выполнить `make index-knowledge` (upsert дозаполняет список).
"""

from alembic import op

revision = "s06_0006_knowledge_refs"
down_revision = "s06_0005_vector_memory"
branch_labels = None
depends_on = None

SCHEMA_SQL = r"""
-- Список таблиц документа: права retrieval проверяются по ВСЕМ (NFR-4,
-- ревью S06 C1); '{}' = общий документ без табличных ссылок
ALTER TABLE agent_knowledge
    ADD COLUMN referenced_tables text[] NOT NULL DEFAULT '{}';

-- Backfill: DDL-документ привязан к единственной таблице — список однозначен;
-- остальные типы дозаполняет индексатор (make index-knowledge), см. docstring
UPDATE agent_knowledge
SET referenced_tables = ARRAY[table_name]
WHERE doc_kind = 'ddl' AND table_name IS NOT NULL;
"""

DROP_SQL = r"""
ALTER TABLE agent_knowledge DROP COLUMN referenced_tables;
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    op.execute(DROP_SQL)
