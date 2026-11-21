"""Гибридная память: FTS-нога (content_tsv + GIN) + doc_kind 'noise' (S26, feat-021).

Канон D-026 (+дополнения, BL-007): ключевая нога — PostgreSQL FTS
(ts_rank по generated-колонке), слияние взвешенным RRF в адаптере; шумовой
корпус-дистрактор — doc_kind='noise' (ранжируется вместе с реальными
документами, в промпт не попадает — фильтр при сборке контекста).

Generated-колонка: конфиг 'russian' — литералом (immutable-требование PG,
Ловушки-3: функция/конфиг с параметрами generated не проходит). Нормализация
контента (снятие U+0301, ё→е) — хелпером ДО записи (text_normalizer):
generated-колонка берёт content как есть.

Правка применённой миграции 0005 запрещена (S00-5, D-028) — расширение CHECK
doc_kind ('noise') живёт здесь, новым файлом (ruling blocker ARCH-GATE S26:
CHECK 0005 не знает 'noise', индексация шума падала бы на constraint).
"""

from alembic import op

revision = "s26_0010_hybrid_memory_fts"
down_revision = "s09_0009_edge_proposals_grant"
branch_labels = None
depends_on = None

SCHEMA_SQL = r"""
-- === Гибридная память: FTS-нога + шумовой корпус (feat-021, BL-007; D-026) ===

-- Ключевая нога: tsvector по содержимому, STORED (пересчёт на UPDATE content —
-- триггер не нужен). Конфиг 'russian' литералом — immutable для generated.
ALTER TABLE agent_knowledge ADD COLUMN content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('russian', content)) STORED;

-- GIN над tsvector — носитель FTS-ноги (@@ без него — последовательный скан)
CREATE INDEX idx_agent_knowledge_content_tsv ON agent_knowledge USING GIN (content_tsv);

-- Новый doc_kind 'noise' (D-026: дистракторы ранжируются вместе с реальными).
-- Переопределение CHECK новым файлом: PG-имя безымянного CHECK 0005 —
-- {table}_{column}_check (agent_knowledge_doc_kind_check).
ALTER TABLE agent_knowledge DROP CONSTRAINT agent_knowledge_doc_kind_check;
ALTER TABLE agent_knowledge ADD CONSTRAINT agent_knowledge_doc_kind_check
    CHECK (doc_kind IN ('ddl', 'relationship', 'glossary', 'few_shot', 'noise'));
"""

DOWNGRADE_SQL = r"""
DROP INDEX idx_agent_knowledge_content_tsv;
ALTER TABLE agent_knowledge DROP COLUMN content_tsv;
ALTER TABLE agent_knowledge DROP CONSTRAINT agent_knowledge_doc_kind_check;
ALTER TABLE agent_knowledge ADD CONSTRAINT agent_knowledge_doc_kind_check
    CHECK (doc_kind IN ('ddl', 'relationship', 'glossary', 'few_shot'));
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
