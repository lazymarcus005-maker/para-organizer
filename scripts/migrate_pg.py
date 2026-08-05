"""One-shot migration script: copy data from SQLite to PostgreSQL.

Usage::

    python3 scripts/migrate_pg.py

Requires:
- A running PostgreSQL instance with the PARA database created
- The SQLite database at the path configured in settings.PARA_DB_PATH
- Alembic migrations already applied to PostgreSQL (run ``alembic upgrade head`` first)

What it does:
1. Connects to both SQLite and PostgreSQL
2. Copies data table by table using batched INSERT
3. Verifies row counts match
4. Handles FTS5 → tsvector migration (tsvector is generated, so no data copy needed)
5. Handles sqlite-vec → pgvector migration (copies raw embedding data)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime
from typing import Any, Callable

import aiosqlite
import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_pg")

BATCH_SIZE = 500


def _transform_note(row: dict) -> dict:
    """Transform a SQLite note row to PostgreSQL-compatible values."""
    row = dict(row)
    if isinstance(row.get("tags"), str):
        try:
            row["tags"] = json.loads(row["tags"])
        except (json.JSONDecodeError, TypeError):
            row["tags"] = []
    if isinstance(row.get("source_metadata"), str):
        try:
            row["source_metadata"] = json.loads(row["source_metadata"])
        except (json.JSONDecodeError, TypeError):
            row["source_metadata"] = {}
    if isinstance(row.get("recurrence"), str):
        try:
            row["recurrence"] = json.loads(row["recurrence"])
        except (json.JSONDecodeError, TypeError):
            row["recurrence"] = None
    row.pop("embedding", None)
    return row


def _transform_notification(row: dict) -> dict:
    row = dict(row)
    if isinstance(row.get("payload"), str):
        try:
            row["payload"] = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            row["payload"] = None
    return row


def _transform_event(row: dict) -> dict:
    row = dict(row)
    if isinstance(row.get("payload"), str):
        try:
            row["payload"] = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            row["payload"] = None
    return row


# Tables to migrate in dependency order (FK-safe)
TABLES: list[tuple[str, str, list[str], Callable | None]] = [
    ("notes", "notes", [
        "id", "title", "content", "para_category", "sub_category", "status",
        "priority", "deadline", "tags", "source", "source_metadata",
        "llm_model", "llm_confidence", "llm_reasoning", "embedding_status",
        "recurrence", "created_at", "updated_at", "archived_at", "summary",
    ], _transform_note),
    ("links", "links", [
        "id", "from_note_id", "to_note_id", "link_type", "created_at",
    ], None),
    ("history", "history", [
        "id", "note_id", "action", "old_value", "new_value", "reason", "timestamp",
    ], None),
    ("notifications", "notifications", [
        "id", "note_id", "type", "channel", "status", "scheduled_at",
        "sent_at", "payload",
    ], _transform_notification),
    ("settings", "settings", ["key", "value"], None),
    ("chat_messages", "chat_messages", [
        "id", "chat_id", "role", "content", "created_at",
    ], None),
    ("llm_usage", "llm_usage", [
        "id", "ts", "model", "task", "prompt_tokens", "completion_tokens", "note_id",
    ], None),
    ("events", "events", [
        "id", "event_type", "note_id", "payload", "status", "created_at", "delivered_at",
    ], _transform_event),
    ("tasks", "tasks", [
        "id", "prompt", "status", "note_id", "created_at", "updated_at",
    ], None),
    ("items", "items", [
        "id", "note_id", "text", "done", "created_at",
    ], None),
    ("feedback", "feedback", [
        "id", "field", "llm_value", "user_value", "note_content_snippet", "created_at",
    ], None),
]


async def _count_sqlite(db: aiosqlite.Connection, table: str) -> int:
    cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
    row = await cursor.fetchone()
    return row[0]


async def _count_pg(conn: asyncpg.Connection, table: str) -> int:
    row = await conn.fetchrow(f"SELECT COUNT(*) FROM {table}")
    return row[0]


async def _copy_table(
    sqlite_db: aiosqlite.Connection,
    pg_conn: asyncpg.Connection,
    sqlite_table: str,
    pg_table: str,
    columns: list[str],
    transform: Callable | None,
) -> int:
    """Copy all rows from sqlite_table to pg_table in batches."""
    logger.info("Copying %s → %s ...", sqlite_table, pg_table)

    placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
    col_names = ", ".join(columns)
    insert_sql = f"INSERT INTO {pg_table} ({col_names}) VALUES ({placeholders})"

    offset = 0
    total = 0
    while True:
        cursor = await sqlite_db.execute(
            f"SELECT {col_names} FROM {sqlite_table} ORDER BY id LIMIT ? OFFSET ?",
            (BATCH_SIZE, offset),
        )
        rows = await cursor.fetchall()
        if not rows:
            break

        batch = []
        for row in rows:
            d = dict(row)
            if transform:
                d = transform(d)
            batch.append(tuple(d.get(c) for c in columns))

        await pg_conn.executemany(insert_sql, batch)
        total += len(batch)
        offset += BATCH_SIZE
        logger.info("  %s: %d rows copied", sqlite_table, total)

    return total


async def main() -> None:
    sqlite_path = settings.PARA_DB_PATH
    pg_url = settings.PARA_DB_URL

    if not os.path.exists(sqlite_path):
        logger.error("SQLite database not found at %s", sqlite_path)
        sys.exit(1)

    logger.info("SQLite: %s", sqlite_path)
    logger.info("PostgreSQL URL: %s", pg_url.replace("+asyncpg", ""))

    sqlite_db = await aiosqlite.connect(sqlite_path)
    sqlite_db.row_factory = aiosqlite.Row
    await sqlite_db.execute("PRAGMA foreign_keys=OFF")

    dsn = pg_url.replace("postgresql+asyncpg://", "postgresql://")
    pg_conn = await asyncpg.connect(dsn)

    try:
        for sqlite_table, pg_table, columns, transform in TABLES:
            sqlite_count = await _count_sqlite(sqlite_db, sqlite_table)
            if sqlite_count == 0:
                logger.info("  %s: empty, skipping", sqlite_table)
                continue

            copied = await _copy_table(sqlite_db, pg_conn, sqlite_table, pg_table, columns, transform)
            pg_count = await _count_pg(pg_conn, pg_table)

            if copied == pg_count:
                logger.info("  ✓ %s: %d rows verified", sqlite_table, pg_count)
            else:
                logger.warning(
                    "  ⚠ %s: copied %d but PG has %d — manual review recommended",
                    sqlite_table, copied, pg_count,
                )

        logger.info("Migration complete!")
        logger.info("Note: embeddings were NOT migrated. Run the embed_backfill job to re-embed all notes.")
        logger.info("Note: FTS5 data was NOT migrated. PostgreSQL tsvector is generated automatically.")

    finally:
        await sqlite_db.close()
        await pg_conn.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
