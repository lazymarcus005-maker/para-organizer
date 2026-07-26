"""Idempotent DB migration runner.

Migrations are ordered modules named NNN_*.py in this package. Each exposes a
`NAME` constant and an ``async def migrate(db) -> tuple[list[str], list[str]]``
that returns (applied, skipped) descriptions. Every migration must be safe to
run repeatedly (CREATE ... IF NOT EXISTS, guarded ALTER TABLE), so init_db() can
call this on every startup, in tests, and for manual admin without error.

Module file names start with a digit, so they're loaded via importlib rather
than a plain ``import`` statement.
"""

from __future__ import annotations

import importlib
import logging

import aiosqlite

logger = logging.getLogger("para.migrations")

# Ordered list of migration module names (without the .py suffix).
_MIGRATION_NAMES = [
    "001_initial_schema",
    "002_add_summary_column",
]

MIGRATIONS = [
    importlib.import_module(f"app.migrations.{name}") for name in _MIGRATION_NAMES
]


async def run_migrations(db: aiosqlite.Connection) -> None:
    """Apply every migration in order, logging what was changed vs. skipped."""
    for migration in MIGRATIONS:
        applied, skipped = await migration.migrate(db)
        logger.info(
            "Migration %s: applied=%s skipped=%s",
            migration.NAME,
            applied or "-",
            skipped or "-",
        )
