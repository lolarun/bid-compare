"""Alembic environment — wired to the app's Base.metadata and DATABASE_URL.

Design: docs/design/13-alembic-migration-introduction.md (Plan B).

- DB URL is taken from apps.api.core.database.DATABASE_URL (never hard-coded),
  unless an engine/connection is injected programmatically via
  config.attributes["connection"] (used by init_db's _run_alembic_upgrade).
- target_metadata = Base.metadata, with all models imported so autogenerate
  and stamping see the full schema.
- render_as_batch=True so SQLite ALTER COLUMN works (batch mode).
"""
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Import the app metadata + URL. Importing apps.api.models registers every
# ORM table on Base.metadata (required for autogenerate / full-schema view).
from apps.api.core.database import Base, DATABASE_URL
import apps.api.models  # noqa: F401 — side-effect: register all models

config = context.config

# Only configure logging from the ini for standalone CLI runs. When init_db()
# drives migrations programmatically it injects a live connection; in that case
# skip fileConfig so we don't clobber the running app's logging setup.
if config.config_file_name is not None and config.attributes.get("connection") is None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' (URL-only) mode."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    If a live connection is injected (config.attributes["connection"]) — the
    path used by init_db()._run_alembic_upgrade() — reuse it so migrations run
    on the same engine the app uses. Otherwise build an engine from
    DATABASE_URL.
    """
    connection = config.attributes.get("connection", None)

    if connection is not None:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = DATABASE_URL
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as conn:
        context.configure(
            connection=conn,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
