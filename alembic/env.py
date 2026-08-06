"""Alembic environment configuration for PARA Organizer PostgreSQL schema.

Uses async SQLAlchemy engine and the ORM models from app.models_v2.
"""
import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

# Ensure the project root is on sys.path so that `app` is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

from app.models_v2 import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def get_database_url() -> str:
    """Resolve the database URL, preferring the PARA_DB_URL env var.

    Reads the env var directly (instead of via config.set_main_option) so URLs
    containing literal '%' characters are not mangled by configparser
    interpolation.
    """
    return os.environ.get("PARA_DB_URL") or config.get_main_option("sqlalchemy.url")

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(get_database_url(), poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
