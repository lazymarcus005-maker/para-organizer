"""Migration 001 — baseline additions for later phases.

Adds the columns and tables that Phase 1-3 features depend on:

- ``notes.embedding_status`` — backfill marker for the embedding job.
- ``notes.recurrence``      — JSON string describing a recurring schedule.
- ``llm_usage`` table       — per-call LLM token/cost accounting.

Idempotent by construction: columns are added only when absent (SQLite has no
``ADD COLUMN IF NOT EXISTS``), and the table/index use ``IF NOT EXISTS``. Safe on
both a fresh DB and an existing production DB, and safe to run many times.
"""

from __future__ import annotations

import aiosqlite

NAME = "001_initial_schema"

LLM_USAGE_SQL = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts DATETIME DEFAULT CURRENT_TIMESTAMP,
    model TEXT NOT NULL,
    task TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    note_id INTEGER,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
"""

# New columns on `notes`: (name, column definition appended after ADD COLUMN).
_NOTES_COLUMNS = [
    ("embedding_status", "TEXT DEFAULT 'pending'"),
    ("recurrence", "TEXT"),
]


async def _column_exists(db: aiosqlite.Connection, table: str, column: str) -> bool:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk).
    return any(row[1] == column for row in rows)


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return await cursor.fetchone() is not None


async def migrate(db: aiosqlite.Connection) -> tuple[list[str], list[str]]:
    applied: list[str] = []
    skipped: list[str] = []

    for column, definition in _NOTES_COLUMNS:
        if await _column_exists(db, "notes", column):
            skipped.append(f"notes.{column}")
        else:
            await db.execute(f"ALTER TABLE notes ADD COLUMN {column} {definition}")
            applied.append(f"notes.{column}")

    if await _table_exists(db, "llm_usage"):
        skipped.append("llm_usage")
    else:
        applied.append("llm_usage")
    # Executed unconditionally (IF NOT EXISTS) so a missing index on a
    # pre-existing table is still created.
    await db.executescript(LLM_USAGE_SQL)

    return applied, skipped
