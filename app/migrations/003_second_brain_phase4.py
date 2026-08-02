"""Migration 003 — Phase 4 Second Brain: events + tasks tables.

Adds:
- ``events`` table for outbound webhook dispatch (SB-01)
- ``tasks`` table for Hermes task delegation (SB-02)
- ``notes.agent_id`` column for multi-agent identity (SB-10 prep)
- ``notes.progress`` column for action-item tracking (SB-06 prep)

Idempotent: uses CREATE TABLE IF NOT EXISTS and guarded ALTER TABLE.
"""

from __future__ import annotations

import aiosqlite

NAME = "003_second_brain_phase4"

EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    note_id INTEGER,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at DATETIME,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
"""

TASKS_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER,
    task_type TEXT NOT NULL DEFAULT 'general',
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    hermes_job_id TEXT,
    agent_id TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_note ON tasks(note_id);
CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(agent_id);
"""

_NOTES_COLUMNS = [
    ("agent_id", "TEXT"),
    ("progress", "REAL DEFAULT NULL"),
]


async def _column_exists(db: aiosqlite.Connection, table: str, column: str) -> bool:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return any(row[1] == column for row in rows)


async def migrate(db: aiosqlite.Connection) -> tuple[list[str], list[str]]:
    applied: list[str] = []
    skipped: list[str] = []

    await db.executescript(EVENTS_SQL)
    applied.append("events")

    await db.executescript(TASKS_SQL)
    applied.append("tasks")

    for column, definition in _NOTES_COLUMNS:
        if await _column_exists(db, "notes", column):
            skipped.append(f"notes.{column}")
        else:
            await db.execute(f"ALTER TABLE notes ADD COLUMN {column} {definition}")
            applied.append(f"notes.{column}")

    return applied, skipped
