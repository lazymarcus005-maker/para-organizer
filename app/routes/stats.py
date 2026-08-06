"""/api/stats, /api/deadlines, /api/digest."""

from datetime import date, datetime, timedelta

import aiosqlite
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.database import get_db
from app.database_v2 import async_session_factory
from app.models import PARA_CATEGORIES, PRIORITIES, STATUSES
from app.models_v2 import Note
from app.usage import usage_summary

router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/usage")
async def get_usage(days: int = Query(default=7, ge=1, le=365)):
    """LLM token usage over the last `days` days (read-only, no auth)."""
    return await usage_summary(days)


@router.get("/stats")
async def get_stats():
    """Aggregate note counts. Reads from PostgreSQL (the notes table's source
    of truth) via the SQLAlchemy/asyncpg session, not the legacy aiosqlite
    connection which points at an uninitialized local SQLite file.
    """
    from app.cache import get_cache
    cache = get_cache()
    cached = await cache.get("para:stats")
    if cached is not None:
        return cached

    async with async_session_factory() as session:
        total_notes = (await session.execute(select(func.count()).select_from(Note))).scalar_one()

        by_category = {}
        for category in PARA_CATEGORIES:
            by_category[category] = (await session.execute(
                select(func.count()).select_from(Note).where(Note.para_category == category)
            )).scalar_one()

        by_status = {}
        for status in STATUSES:
            by_status[status] = (await session.execute(
                select(func.count()).select_from(Note).where(Note.status == status)
            )).scalar_one()

        by_priority = {}
        for priority in PRIORITIES:
            by_priority[priority] = (await session.execute(
                select(func.count()).select_from(Note).where(Note.priority == priority)
            )).scalar_one()

        upcoming_deadlines = (await session.execute(
            select(func.count()).select_from(Note).where(
                Note.deadline.isnot(None), Note.deadline >= date.today()
            )
        )).scalar_one()

        avg_confidence_raw = (await session.execute(
            select(func.avg(Note.llm_confidence)).where(Note.llm_confidence > 0)
        )).scalar_one()
        avg_confidence = round(avg_confidence_raw, 3) if avg_confidence_raw is not None else 0.0

    result = {
        "total_notes": total_notes,
        "by_category": by_category,
        "by_status": by_status,
        "by_priority": by_priority,
        "upcoming_deadlines": upcoming_deadlines,
        "avg_confidence": avg_confidence,
    }
    await cache.set("para:stats", result, ttl=60)
    return result


@router.get("/deadlines")
async def get_deadlines(
    days: int = Query(default=14, ge=1, le=365),
    db: aiosqlite.Connection = Depends(get_db),
):
    from app.cache import get_cache
    cache = get_cache()
    cache_key = f"para:deadlines:{days}"
    cached = await cache.get(cache_key)
    if cached is not None:
        return cached
    horizon = (date.today() + timedelta(days=days)).isoformat()
    cursor = await db.execute(
        """
        SELECT id, title, deadline, priority FROM notes
        WHERE deadline IS NOT NULL AND deadline >= date('now') AND deadline <= ?
        ORDER BY deadline ASC
        """,
        (horizon,),
    )
    rows = await cursor.fetchall()

    today = date.today()
    deadlines = []
    for row in rows:
        d = row["deadline"]
        deadline_date = date.fromisoformat(d) if isinstance(d, str) else d
        deadlines.append({
            "id": row["id"],
            "title": row["title"],
            "deadline": d,
            "days_left": (deadline_date - today).days,
            "priority": row["priority"],
        })

    result = {"deadlines": deadlines}
    await cache.set(cache_key, result, ttl=120)
    return result


@router.get("/digest")
async def get_digest(db: aiosqlite.Connection = Depends(get_db)):
    from app.cache import get_cache
    cache = get_cache()
    from datetime import date as dt_date
    cache_key = f"para:digest:{dt_date.today().isoformat()}"
    cached = await cache.get(cache_key)
    if cached is not None:
        return cached
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()

    total_cursor = await db.execute("SELECT COUNT(*) AS c FROM notes")
    total_notes = (await total_cursor.fetchone())["c"]

    by_category = {}
    for category in PARA_CATEGORIES:
        cursor = await db.execute("SELECT COUNT(*) AS c FROM notes WHERE para_category = ?", (category,))
        by_category[category] = (await cursor.fetchone())["c"]

    completed_cursor = await db.execute(
        "SELECT id, title FROM notes WHERE status = 'archived' AND archived_at >= ?", (week_ago,)
    )
    completed_this_week = [dict(r) for r in await completed_cursor.fetchall()]

    active_cursor = await db.execute(
        "SELECT id, title, deadline FROM notes WHERE para_category = 'projects' AND status = 'active'"
    )
    active_projects = [dict(r) for r in await active_cursor.fetchall()]

    new_cursor = await db.execute("SELECT COUNT(*) AS c FROM notes WHERE created_at >= ?", (week_ago,))
    new_notes_count = (await new_cursor.fetchone())["c"]

    result = {
        "total_notes": total_notes,
        "by_category": by_category,
        "completed_this_week": completed_this_week,
        "active_projects": active_projects,
        "new_notes_count": new_notes_count,
    }
    await cache.set(cache_key, result, ttl=300)
    return result
