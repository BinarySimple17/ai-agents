"""Миграция 002 (S02): роли БД + whitelist-гранты + hardening.

Источники (канон):
- роли и пароли из .env — security.md §2.1/§2.2, S00 правило 4 (секретов-дефолтов в коде нет);
- agent_ro: default_transaction_read_only + statement_timeout на роль — ADR-005 §4, рубеж 5;
- гранты — whitelist, deny-by-default (FIX-007): SELECT только на 7 бизнес-таблиц,
  query_trace и agent_* нечитаемы без явного REVOKE; ALTER DEFAULT PRIVILEGES исключён;
- agent_trace — второй SQL-канал, INSERT-only на query_trace (security.md §2.2);
  append-only дополнительно гарантирован триггером (миграция 0001);
- registry_writer — INSERT/UPDATE на реестр и заявки, без DELETE (устаревание вместо
  удаления — ADR-006 §4.2);
- dblink/file_fdw не устанавливаются вовсе (security.md §2.1) — проверяется тестом.

RLS-политики — НЕ здесь: переносятся в S13 вместе с механизмом SET LOCAL app.user_id
(FIX-007, решение canon-check ARCH-GATE).
"""

import os

from alembic import op

revision = "s02_0002_roles_hardening"
down_revision = "s02_0001_schema_v15"
branch_labels = None
depends_on = None

# Роль → пароль из окружения (fail-fast: секретов-дефолтов в коде нет — S00, правило 4)
ROLE_ENV_PASSWORDS = {
    "agent_ro": "AGENT_RO_PASSWORD",
    "agent_planner": "AGENT_PLANNER_PASSWORD",
    "agent_trace": "AGENT_TRACE_PASSWORD",
    "registry_writer": "REGISTRY_WRITER_PASSWORD",
}


def _sql_literal(value: str) -> str:
    """Экранирование строкового литерала для SQL (пароли приходят из .env)."""
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    passwords = {}
    for role, env_var in ROLE_ENV_PASSWORDS.items():
        value = os.environ.get(env_var)
        if not value:
            raise RuntimeError(
                f"{env_var} не задан — скопируйте .env.example в .env "
                "(fail-fast: пароли ролей только из .env — S00 правило 4, security.md §2.1)."
            )
        passwords[role] = _sql_literal(value)

    # Роли — кластерные объекты (не per-database): создание условное, чтобы
    # повторный прогон на другом инстансе БД того же кластера не падал
    # DuplicateObject; пароль существующей роли синхронизируется с .env.
    for role in ROLE_ENV_PASSWORDS:
        op.execute(
            f"DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN "
            f"CREATE ROLE {role} LOGIN PASSWORD {passwords[role]} NOSUPERUSER NOINHERIT; "
            f"ELSE ALTER ROLE {role} WITH LOGIN PASSWORD {passwords[role]}; "
            f"END IF; END $$;"
        )

    # --- Канал исполнения (security.md §2.1): read-only на уровне сессии ---
    op.execute("ALTER ROLE agent_ro SET default_transaction_read_only = on")
    op.execute("ALTER ROLE agent_ro SET statement_timeout = '30s'")
    op.execute("GRANT USAGE ON SCHEMA public TO agent_ro")
    # whitelist (FIX-007): ни query_trace, ни agent_* нечитаемы — гранта нет по умолчанию
    op.execute(
        "GRANT SELECT ON customers, campaigns, orders, order_items, "
        "products, employees, bonuses TO agent_ro"
    )

    # --- Планировщик (ADR-005 §4, рубеж 5: раздельные коннекты планировщик/исполнитель) ---
    op.execute("GRANT USAGE ON SCHEMA public TO agent_planner")
    op.execute(
        "GRANT SELECT ON customers, campaigns, orders, order_items, "
        "products, employees, bonuses TO agent_planner"
    )
    op.execute(
        "GRANT SELECT ON agent_permissions, agent_user_roles, agent_data_sources, "
        "settings_version TO agent_planner"
    )
    # ADR-006 §4.2: агент читает реестр ТОЛЬКО через вьюху active — базовая
    # agent_relationships и agent_edge_proposals (question_sample — чужие вопросы)
    # планировщику не выдаются (ревью Important-3)
    op.execute("GRANT SELECT ON agent_relationships_active TO agent_planner")

    # --- Канал записи трейса (security.md §2.2): INSERT-only ---
    op.execute("GRANT USAGE ON SCHEMA public TO agent_trace")
    op.execute("GRANT INSERT ON query_trace TO agent_trace")
    # SELECT/UPDATE/DELETE/TRUNCATE не выдаются вовсе: append-only = отсутствие грантов

    # --- Владелец реестра (ADR-006 §4.2): запись через ревью, без DELETE ---
    op.execute("GRANT USAGE ON SCHEMA public TO registry_writer")
    op.execute(
        "GRANT INSERT, UPDATE ON agent_relationships, agent_edge_proposals TO registry_writer"
    )


def downgrade() -> None:
    # DROP ROLE падает, пока у роли есть гранты в БД (ревью Important-2):
    # DROP OWNED снимает все привилегии роли в ТЕКУЩЕЙ БД, затем роль удаляется.
    for role in ROLE_ENV_PASSWORDS:
        op.execute(f"DROP OWNED BY {role}")
        op.execute(f"DROP ROLE IF EXISTS {role}")
