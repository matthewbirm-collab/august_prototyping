"""Alembic environment: runs migrations against DATABASE_URL from app.settings."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import db.models  # noqa: F401  (registers tables on Base.metadata)
from app.settings import get_settings
from db.base import Base

config = context.config
if config.config_file_name is not None:
    # Keep loggers that already exist (the app's, when migrations run in-process) enabled.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def database_url() -> str:
    """`sqlalchemy.url` set by the caller (tests) wins over DATABASE_URL from settings."""
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
