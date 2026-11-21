r"""Миграция 008 (S07 Task 3, feat-007): семантика first-touch демо-ребра атрибуции.

Правка применённой миграции запрещена (S00 правило 5, D-028) — дополнение
семантики сида 0003 идёт НОВОЙ data-миграцией. Демо-вопрос ДЗ-04 «клиенты,
пришедшие из рекламы» обязан раскрывать допущение атрибуции через `usage_notes`
(ADR-006 §4.7 — каноничный пример ребра): сид 0003 ставил в usage_notes только
ловушку NULL campaign_id, допущение first-touch отсутствовало — графовая сессия
(S07) выводит семантику путей (`path_semantics`) прямо из реестра, «клиенты из
рекламы» отвечался бы any-touch молча.

Дисциплина правки ребра (ADR-006 §4.1): ревизия = version + 1 (не DELETE);
updated reviewed_at. Триггер bump_settings_version инкрементирует счётчик
'relationships' → graph_version следующего синка вырастает (reconciliation
и кэш-инвалидация — через pин graph_version на прогон, S00 контракт 5).

Инвариант текста: формулировка first-touch/any-touch из §4.7 ADR-006 —
её ловит регрессионный тест test_join_graph.py (семантика пути атрибуции).
"""

from alembic import op

revision = "s07_0008_demo_edge_first_touch"
down_revision = "s06_0007_knowledge_refs_backfill"
branch_labels = None
depends_on = None

# Атрибуция демо-вопроса ДЗ-04: «пришли из рекламы» = first-touch (первый заказ
# клиента из ads-кампании); any-touch завышает цифру (ADR-006 §4.7).
_FIRST_TOUCH_SQL = r"""
UPDATE agent_relationships
SET version = version + 1,
    usage_notes = '«пришли из рекламы» = first-touch: первый заказ клиента из '
                  'ads-кампании; any-touch завышает цифру; campaign_id надёжен '
                  'с 2026-03; NULL campaign_id — заказы вне кампаний, INNER JOIN '
                  'их молча теряет (ADR-006 §4.7)',
    reviewed_at = now()
WHERE edge_id = 'e_campaigns_orders'
"""

# Откат — к сиду 0003 (version возвращается к 1: инвариант «+1» — про вперёд,
# downgrade восстанавливает исходную ревизию демо-данных).
_ROLLBACK_SQL = r"""
UPDATE agent_relationships
SET version = 1,
    usage_notes = 'campaign_id NULLABLE: заказы вне кампаний молча теряются в INNER JOIN —'
                  ' join_type содержательное решение (ADR-006)',
    reviewed_at = now()
WHERE edge_id = 'e_campaigns_orders'
"""


def upgrade() -> None:
    op.execute(_FIRST_TOUCH_SQL)


def downgrade() -> None:
    op.execute(_ROLLBACK_SQL)
