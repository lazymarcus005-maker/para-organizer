"""Multi-agent memory helpers (SB-10): identity tracking across shared PARA brain."""

from __future__ import annotations

import logging

from app.database import get_connection
from app.utils import row_to_note

logger = logging.getLogger("para.agents")


async def get_agent_summary(agent_id: str) -> dict:
    async with get_connection() as db:
        notes_row = await (await db.execute(
            "SELECT COUNT(*) AS c FROM notes WHERE agent_id = ?", (agent_id,)
        )).fetchone()

        tasks_row = await (await db.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS done,
                      SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
               FROM tasks WHERE agent_id = ?""",
            (agent_id,),
        )).fetchone()

        last_row = await (await db.execute(
            """SELECT MAX(ts) AS last_active FROM (
                   SELECT MAX(updated_at) AS ts FROM notes WHERE agent_id = ?
                   UNION ALL
                   SELECT MAX(COALESCE(completed_at, created_at)) AS ts FROM tasks WHERE agent_id = ?
               )""",
            (agent_id, agent_id),
        )).fetchone()

        cat_rows = await (await db.execute(
            """SELECT para_category, COUNT(*) AS c FROM notes
               WHERE agent_id = ? GROUP BY para_category ORDER BY c DESC""",
            (agent_id,),
        )).fetchall()

    return {
        "agent_id": agent_id,
        "notes_count": notes_row["c"],
        "tasks_count": tasks_row["total"] or 0,
        "tasks_completed": tasks_row["done"] or 0,
        "tasks_failed": tasks_row["failed"] or 0,
        "last_active": last_row["last_active"],
        "top_categories": [row["para_category"] for row in cat_rows],
    }


async def detect_conflicts(days: int = 7) -> list[dict]:
    since = f"-{int(days)} days"
    async with get_connection() as db:
        rows = await (await db.execute(
            """SELECT note_id,
                      GROUP_CONCAT(DISTINCT new_value) AS sources,
                      MAX(timestamp) AS last_conflict
               FROM history
               WHERE action = 'created' AND timestamp >= datetime('now', ?)
               GROUP BY note_id
               HAVING COUNT(DISTINCT new_value) > 1""",
            (since,),
        )).fetchall()

        conflicts = []
        for row in rows:
            title_row = await (await db.execute(
                "SELECT title FROM notes WHERE id = ?", (row["note_id"],)
            )).fetchone()
            conflicts.append({
                "note_id": row["note_id"],
                "title": title_row["title"] if title_row else None,
                "sources": [s for s in (row["sources"] or "").split(",") if s],
                "last_conflict": row["last_conflict"],
            })

    return conflicts


async def get_shared_notes(limit: int = 20) -> list[dict]:
    async with get_connection() as db:
        rows = await (await db.execute(
            """SELECT * FROM notes WHERE agent_id IS NULL AND status = 'active'
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        )).fetchall()

    return [row_to_note(r) for r in rows]
