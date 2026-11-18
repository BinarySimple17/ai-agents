"""Alembic Environment — схема БД создаётся ТОЛЬКО миграциями (S00, правило 5).

DSN — из окружения (POSTGRES_DSN из .env; fail-fast, без дефолта с секретом).
Автогенерация off: миграции пишутся вручную под er-data-model.md (docs/diagrams/);
когда появится Settings (feat-003), чтение env переезжает на неё.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

dsn = os.environ.get("POSTGRES_DSN")
if not dsn:
    raise RuntimeError(
        "POSTGRES_DSN не задан — скопируйте .env.example в .env и заполните "
        "(fail-fast: секретов-дефолтов в коде нет — S00, правило 4)."
    )
config.set_main_option("sqlalchemy.url", dsn)

# Миграции рукописные (схема = er-data-model.md), autogenerate не используется
target_metadata = None


def run_migrations_offline() -> None:
    """Offline-режим: сгенерировать SQL без подключения (alembic upgrade head --sql)."""
    context.configure(
        url=dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Online-режим: стандартный прогон миграций с подключением."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
