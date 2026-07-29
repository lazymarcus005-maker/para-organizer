"""Migration 004 — Phase 5 Intelligence: action_items + feedback tables.

Adds:
- ``action_items`` table for subtask tracking (SB-06)
- ``feedback`` table for classification feedback loop (SB-08)

Idempotent: uses CREATE TABLE IF NOT EXISTS.
"""

from __future__ import annotations

import aiosqlite

NAME = "004_second_brain_phase5"

ACTION_ITEMS_SQL = """
CREATE TABLE IF NOT EXISTS action_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo',
    order_index INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_action_items_note ON action_items(note_id);
CREATE INDEX IF NOT EXISTS idx_action_items_status ON action_items(status);
"""

FEEDBACK_SQL = """
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    llm_value TEXT,
    user_value TEXT NOT NULL,
    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_feedback_note ON feedback(note_id);
CREATE INDEX IF NOT EXISTS idx_feedback_field ON feedback(field);
CREATE INDEX IF NOT EXISTS idx_feedback_ts ON feedback(timestamp);
"""


async def migrate(db: aiosqlite.Connection) -> tuple[list[str], list[str]]:
    applied: list[str] = []
    skipped: list[str] = []

    await db.executescript(ACTION_ITEMS_SQL)
    applied.append("action_items")

    await db.executescript(FEEDBACK_SQL)
    applied.append("feedback")

    return applied, skipped
