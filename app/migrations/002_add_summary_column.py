"""Add summary column to notes table for note distillation on archive."""

import aiosqlite

NAME = "002_add_summary_column"


async def migrate(db: aiosqlite.Connection) -> tuple[list[str], list[str]]:
    """Add summary column if it doesn't exist (idempotent)."""
    applied = []
    skipped = []
    
    # Check if summary column already exists
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("PRAGMA table_info(notes)")
    rows = await cursor.fetchall()
    columns = {row["name"] for row in rows}
    
    if "summary" not in columns:
        await db.execute("ALTER TABLE notes ADD COLUMN summary TEXT DEFAULT NULL")
        applied.append("Added summary column to notes table")
    else:
        skipped.append("summary column already exists")
    
    await db.commit()
    return applied, skipped
