"""Миграция 004 (S13): RLS-политики чувствительных таблиц — рубеж 5 (FIX-007).

Перенос из S02 по FIX-007 (ruling ARCH-GATE S13 Q2): механизм `SET LOCAL
app.user_id` появляется в канале исполнения, поэтому и политики — здесь.

Дизайн (ruling Q2):
- чувствительные таблицы — employees и bonuses (er-data-model: salary —
  PII-колонка, «премии» — кейс deny); per-user deny остаётся за рубежом 2
  (права как данные) — RLS дублирует лишь грубое правило «исполнение всегда
  с user_id» (ADR-005 §4);
- форма — fail-closed tripwire: FOR SELECT TO agent_ro
  USING (current_setting('app.user_id', true) IS NOT NULL) — без выставленного
  user_id agent_ro видит 0 строк;
- ENABLE без FORCE: владелец таблиц postgres (миграции) не обрезается — иначе
  сломался бы test_infra_schema; agent_ro — не владелец, ENABLE достаточен;
  политика TO agent_ro: любой другой не-владелец с SELECT-грантом (например
  agent_planner) теперь молча видит 0 строк — deny-by-default RLS, не поломка
  (примечание для S07);
- колоночный PII (salary) RLS не решает — S05/S11.
"""

from alembic import op

revision = "s13_0004_rls_sensitive_tables"
down_revision = "s02_0003_demo_data"
branch_labels = None
depends_on = None

_SENSITIVE_TABLES = ("employees", "bonuses")
_POLICY_NAME = "agent_ro_requires_user_id"


def upgrade() -> None:
    for table in _SENSITIVE_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {_POLICY_NAME} ON {table} "
            "FOR SELECT TO agent_ro "
            "USING (current_setting('app.user_id', true) IS NOT NULL)"
        )


def downgrade() -> None:
    for table in _SENSITIVE_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {_POLICY_NAME} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
