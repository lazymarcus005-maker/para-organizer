"""Context injection (SB-03): build a compact situational context package for
an agent working on a topic — related notes, near-term deadlines, pending tasks,
recent activity, graph neighbors, and quick stats."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import aiosqlite

from app.chat import _hybrid_retrieve
from app.database import get_connection

logger = logging.getLogger("para.context")


async def _related_notes(topic: str, db: aiosqlite.Connection, limit: int) -> list[dict]:
    rows = await _hybrid_retrieve(topic, db)
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "para_category": row["para_category"],
            "snippet": (row["content"] or "")[:200],
            "relevance": round(1.0 / (rank + 1.0), 4),
        }
        for rank, row in enumerate(rows[:limit])
    ]


async def _upcoming_deadlines(db: aiosqlite.Connection, limit: int) -> list[dict]:
    horizon = (date.today() + timedelta(days=14)).isoformat()
    rows = await (await db.execute(
        """SELECT id, title, deadline FROM notes
           WHERE status = 'active' AND deadline BETWEEN date('now') AND ?
           ORDER BY deadline LIMIT ?""",
        (horizon, limit),
    )).fetchall()
    result: list[dict] = []
    for row in rows:
        try:
            days_left = (date.fromisoformat(str(row["deadline"])[:10]) - date.today()).days
        except (ValueError, TypeError):
            days_left = None
        result.append({
            "id": row["id"],
            "title": row["title"],
            "deadline": row["deadline"],
            "days_left": days_left,
        })
    return result


async def _pending_tasks(db: aiosqlite.Connection, limit: int) -> list[dict]:
    rows = await (await db.execute(
        """SELECT id, prompt, status, note_id FROM tasks
           WHERE status IN ('pending', 'dispatched')
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    )).fetchall()
    return [dict(row) for row in rows]


async def _recent_activity(db: aiosqlite.Connection, limit: int) -> list[dict]:
    rows = await (await db.execute(
        """SELECT id, title, para_category, updated_at FROM notes
           WHERE updated_at >= datetime('now', '-7 days')
           ORDER BY updated_at DESC LIMIT ?""",
        (limit,),
    )).fetchall()
    return [dict(row) for row in rows]


async def _graph_neighbors(note_ids: list[int], db: aiosqlite.Connection) -> list[dict]:
    if not note_ids:
        return []
    placeholders = ",".join("?" * len(note_ids))
    rows = await (await db.execute(
        f"""SELECT n.id, n.title, l.link_type FROM links l
            JOIN notes n ON n.id = l.to_note_id
            WHERE l.from_note_id IN ({placeholders})
            UNION
            SELECT n.id, n.title, l.link_type FROM links l
            JOIN notes n ON n.id = l.from_note_id
            WHERE l.to_note_id IN ({placeholders})""",
        [*note_ids, *note_ids],
    )).fetchall()
    seen = set(note_ids)
    neighbors: list[dict] = []
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        neighbors.append({
            "id": row["id"],
            "title": row["title"],
            "link_type": row["link_type"],
        })
    return neighbors


async def _quick_stats(db: aiosqlite.Connection) -> dict:
    total = (await (await db.execute("SELECT COUNT(*) c FROM notes")).fetchone())["c"]
    rows = await (await db.execute(
        "SELECT para_category, COUNT(*) c FROM notes GROUP BY para_category"
    )).fetchall()
    by_category = {row["para_category"]: row["c"] for row in rows}
    return {
        "total_notes": total,
        "by_category": by_category,
        "inbox_count": by_category.get("inbox", 0),
    }


async def build_context(topic: str, limit: int = 5) -> dict:
    logger.info("Building context for topic %r (limit=%s)", topic, limit)
    context: dict = {
        "topic": topic,
        "related_notes": [],
        "upcoming_deadlines": [],
        "pending_tasks": [],
        "recent_activity": [],
        "graph_neighbors": [],
        "quick_stats": {},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    async with get_connection() as db:
        try:
            context["related_notes"] = await _related_notes(topic, db, limit)
        except Exception:
            logger.warning("related_notes retrieval failed", exc_info=True)
        try:
            context["upcoming_deadlines"] = await _upcoming_deadlines(db, limit)
        except Exception:
            logger.warning("upcoming_deadlines retrieval failed", exc_info=True)
        try:
            context["pending_tasks"] = await _pending_tasks(db, limit)
        except Exception:
            logger.warning("pending_tasks retrieval failed", exc_info=True)
        try:
            context["recent_activity"] = await _recent_activity(db, limit)
        except Exception:
            logger.warning("recent_activity retrieval failed", exc_info=True)
        try:
            related_ids = [note["id"] for note in context["related_notes"]]
            context["graph_neighbors"] = await _graph_neighbors(related_ids, db)
        except Exception:
            logger.warning("graph_neighbors retrieval failed", exc_info=True)
        try:
            context["quick_stats"] = await _quick_stats(db)
        except Exception:
            logger.warning("quick_stats retrieval failed", exc_info=True)
    return context
