"""Миграция 0009 — GRANT записи заявок для agent_planner (S09 Task 4; BL-003 п.2).

«Нет активного пути» → заявка в agent_edge_proposals пишется кодом узла
plan_joins под ролью agent_planner (ruling Q1 ARCH-GATE S09):

- INSERT на таблицу: append-only — UPDATE/DELETE/SELECT не выдаются (прецедент
  канала agent_trace, миграция 0002:92); приватность «question_sample — чужие
  вопросы» из 0002 не задета — INSERT не читает чужие строки;
- USAGE на serial-последовательность: без него INSERT падает с permission
  denied (proposal_id DEFAULT nextval(...));
- hits агент не инкрементит — частота = COUNT(*) строк для владельца (ruling Q1:
  ON CONFLICT DO UPDATE потребовал бы UPDATE-привилегию).

Смена грани ролей (planner получает одну узкую запись) — D-030 в DECISIONS.md;
sync — er-data-model.md v1.9 + security.md §2 тем же коммитом.
"""

from alembic import op

revision = "s09_0009_edge_proposals_grant"
down_revision = "s07_0008_demo_edge_first_touch"


def upgrade() -> None:
    # INSERT-only: append-only очереди = отсутствие остальных грантов (ADR-006 §4.4:
    # агент пишет только в очередь заявок, реестр — по-прежнему только владелец).
    op.execute("GRANT INSERT ON agent_edge_proposals TO agent_planner")
    # serial PK: DEFAULT nextval(sequence) требует USAGE на последовательность.
    op.execute("GRANT USAGE ON SEQUENCE agent_edge_proposals_proposal_id_seq TO agent_planner")


def downgrade() -> None:
    op.execute("REVOKE USAGE ON SEQUENCE agent_edge_proposals_proposal_id_seq FROM agent_planner")
    op.execute("REVOKE INSERT ON agent_edge_proposals FROM agent_planner")
