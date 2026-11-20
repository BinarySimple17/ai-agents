r"""Миграция 007 (S06, fix-волна ревью-2 Important-2): content-based backfill referenced_tables.

Миграция 006 заполнила `referenced_tables` только для `doc_kind='ddl'` —
relationship/glossary/few_shot строки оставались `'{}'` (публичными) до ручного
`make index-knowledge`: на апгрейде уже проиндексированной БД это окно
расширения прав относительно до-фиксового якорного фильтра (ревью S06-2 I2).

Здесь список восстанавливается из самого `content` для ВСЕХ строк: каждое
упоминание бизнес-таблицы (слово-целиком, `\m…\M`) попадает в
`referenced_tables`; `'{}'` — только если ни одна таблица не упомянута.
Направление безопасное: лишнее имя в списке только сужает доступ (до
переиндексации DDL-контент ещё несёт REFERENCES-фрагменты — те строки временно
скрыты от «не-владельцев» вместо расширения прав; `make index-knowledge`
после апгрейда актуализирует список по санитизированному контенту).

Инвариант NFR-4 ACL-leak = 0 держится сразу после `make infra-migrate`,
без обязательного ручного шага. Идемпотентен: повторный UPDATE даёт те же
значения. `_BACKFILL_SQL` вынесен в константу — регрессионный тест гоняет её
на промежуточном состоянии строки (см. test_vector_memory.py).
"""

from alembic import op

revision = "s06_0007_knowledge_refs_backfill"
down_revision = "s06_0006_knowledge_refs"
branch_labels = None
depends_on = None

# Список таблиц — канон 7 бизнес-таблиц демо-схемы (миграция 0001; сверяется
# с BUSINESS_TABLES в infrastructure/memory/knowledge_sources.py — BL-009 M1)
_BACKFILL_SQL = r"""
UPDATE agent_knowledge
SET referenced_tables = COALESCE((
    SELECT array_agg(DISTINCT (m[1]))
    FROM regexp_matches(
        content,
        '\m(customers|campaigns|products|orders|order_items|employees|bonuses)\M',
        'g'
    ) AS m
), '{}')
"""

ROLLBACK_SQL = r"""
UPDATE agent_knowledge
SET referenced_tables = CASE WHEN doc_kind = 'ddl' AND table_name IS NOT NULL
                             THEN ARRAY[table_name] ELSE '{}' END
"""


def upgrade() -> None:
    op.execute(_BACKFILL_SQL)


def downgrade() -> None:
    op.execute(ROLLBACK_SQL)
