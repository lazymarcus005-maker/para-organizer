"""SB-07 — Temporal reasoning + planner.

Builds a suggested plan for the next N days (deadline-driven priorities, stale
projects to revisit, in-progress work) and analyzes the user's historical activity
patterns. Works against either backend (PostgreSQL in production, SQLite in
local-dev / legacy mode).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import and_, select

from app.classifier import call_ollama
from app.config import settings
from app.database_v2 import async_session_factory
from app.models_v2 import History as PgHistory
from app.models_v2 import Note as PgNote

logger = logging.getLogger("para.planner")

_PRIORITY_WEIGHT = {"low": 1, "medium": 2, "high": 3, "urgent": 4}

_DAY_NAMES = {"0": "sun", "1": "mon", "2": "tue", "3": "wed", "4": "thu", "5": "fri", "6": "sat"}
_DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _using_pg() -> bool:
    return bool(settings.PARA_DB_URL and "postgresql" in settings.PARA_DB_URL)


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _urgency_key(item: dict) -> tuple:
    has_deadline = 0 if item["days_left"] is not None else 1
    days = item["days_left"] if item["days_left"] is not None else 9999
    return (has_deadline, days, -_PRIORITY_WEIGHT.get(item["priority"], 2))


# ── Plan generation ────────────────────────────────────────────────────────


async def generate_plan(horizon_days: int = 7) -> dict:
    today = date.today()
    now = datetime.now()
    horizon_date = (today + timedelta(days=horizon_days)).isoformat()
    stale_cutoff = (now - timedelta(days=settings.NOTIFY_STALE_DAYS)).isoformat(sep=" ")

    if _using_pg():
        deadline_rows, stale_rows, progress_rows = await _gather_plan_pg(
            today, horizon_date, stale_cutoff
        )
    else:
        from app.database import get_connection
        async with get_connection() as db:
            deadline_rows = await (await db.execute(
                """SELECT id, title, priority, deadline FROM notes
                   WHERE status='active' AND deadline IS NOT NULL
                     AND deadline >= date('now') AND deadline <= ?
                   ORDER BY deadline ASC""",
                (horizon_date,),
            )).fetchall()
            stale_rows = await (await db.execute(
                """SELECT id, title, updated_at FROM notes
                   WHERE para_category='projects' AND status='active' AND updated_at < ?
                   ORDER BY updated_at""",
                (stale_cutoff,),
            )).fetchall()
            progress_rows = await (await db.execute(
                """SELECT DISTINCT n.id, n.title, n.priority, n.deadline, n.progress
                   FROM notes n
                   JOIN action_items ai ON ai.note_id = n.id
                   WHERE n.status='active' AND (n.progress IS NULL OR n.progress < 100)""",
            )).fetchall()

    prioritized: list[dict] = []
    seen: set[int] = set()

    for row in deadline_rows:
        rid = row["id"] if isinstance(row, dict) else row.id
        rtitle = row["title"] if isinstance(row, dict) else row.title
        rpriority = row["priority"] if isinstance(row, dict) else row.priority
        rdeadline = row["deadline"] if isinstance(row, dict) else row.deadline
        deadline_date = _parse_date(rdeadline)
        days_left = (deadline_date - today).days if deadline_date else None
        prioritized.append({
            "note_id": rid, "title": rtitle, "reason": "deadline approaching",
            "priority": rpriority, "deadline": str(rdeadline) if rdeadline else None,
            "days_left": days_left,
        })
        seen.add(rid)

    for row in progress_rows:
        rid = row["id"] if isinstance(row, dict) else row.id
        if rid in seen:
            continue
        rtitle = row["title"] if isinstance(row, dict) else row.title
        rpriority = row["priority"] if isinstance(row, dict) else row.priority
        rdeadline = row["deadline"] if isinstance(row, dict) else row.deadline
        rprogress = row["progress"] if isinstance(row, dict) else row.progress
        progress = int(rprogress) if rprogress is not None else 0
        deadline_date = _parse_date(rdeadline)
        days_left = (deadline_date - today).days if deadline_date else None
        prioritized.append({
            "note_id": rid, "title": rtitle, "reason": f"in progress, {progress}% done",
            "priority": rpriority, "deadline": str(rdeadline) if rdeadline else None,
            "days_left": days_left,
        })
        seen.add(rid)

    prioritized.sort(key=_urgency_key)

    stale_to_revisit: list[dict] = []
    for row in stale_rows:
        rid = row["id"] if isinstance(row, dict) else row.id
        rtitle = row["title"] if isinstance(row, dict) else row.title
        rupdated = row["updated_at"] if isinstance(row, dict) else row.updated_at
        updated = _parse_dt(rupdated)
        days_stale = (now - updated).days if updated else settings.NOTIFY_STALE_DAYS
        stale_to_revisit.append({
            "note_id": rid, "title": rtitle, "days_stale": days_stale,
        })

    suggested_focus = await _suggest_focus(prioritized, stale_to_revisit)

    return {
        "horizon_days": horizon_days,
        "period": {"start": today.isoformat(), "end": horizon_date},
        "prioritized_actions": prioritized,
        "stale_to_revisit": stale_to_revisit,
        "suggested_focus": suggested_focus,
        "generated_at": now.isoformat(),
    }


async def _gather_plan_pg(today: date, horizon_date: str, stale_cutoff: str):
    horizon_dt = date.fromisoformat(horizon_date)
    stale_dt = datetime.fromisoformat(stale_cutoff)
    async with async_session_factory() as session:
        deadline_rows = (await session.execute(
            select(PgNote.id, PgNote.title, PgNote.priority, PgNote.deadline)
            .where(and_(
                PgNote.status == "active",
                PgNote.deadline.isnot(None),
                PgNote.deadline >= today,
                PgNote.deadline <= horizon_dt,
            ))
            .order_by(PgNote.deadline.asc())
        )).all()
        stale_rows = (await session.execute(
            select(PgNote.id, PgNote.title, PgNote.updated_at)
            .where(and_(
                PgNote.para_category == "projects",
                PgNote.status == "active",
                PgNote.updated_at < stale_dt,
            ))
            .order_by(PgNote.updated_at)
        )).all()
        # Progress rows: notes that have action items. Production schema (per
        # alembic 0001) doesn't have a `progress` column on notes — we treat
        # any note with at least one item as "in progress" if status='active'.
        from app.models_v2 import Item as PgItem
        progress_rows = (await session.execute(
            select(PgNote.id, PgNote.title, PgNote.priority, PgNote.deadline)
            .where(PgNote.status == "active")
            .where(select(PgItem.id).where(PgItem.note_id == PgNote.id).exists())
        )).all()
    # Normalize ORM rows to dicts so the unified code below works.
    return (
        [{"id": r.id, "title": r.title, "priority": r.priority, "deadline": r.deadline}
         for r in deadline_rows],
        [{"id": r.id, "title": r.title, "updated_at": r.updated_at}
         for r in stale_rows],
        [{"id": r.id, "title": r.title, "priority": r.priority, "deadline": r.deadline, "progress": 50}
         for r in progress_rows],
    )


# ── LLM focus summary ──────────────────────────────────────────────────────


def _fallback_focus(prioritized: list[dict], stale: list[dict]) -> str:
    if prioritized:
        return f"โฟกัสงานสำคัญ: {prioritized[0]['title']}"
    if stale:
        return f"รื้อฟื้นโปรเจกต์ที่ค้าง: {stale[0]['title']}"
    return "ไม่มีงานเร่งด่วนในช่วงนี้ ทบทวนเป้าหมายระยะยาวได้"


async def _suggest_focus(prioritized: list[dict], stale: list[dict]) -> str:
    if not prioritized and not stale:
        return _fallback_focus(prioritized, stale)

    lines = ["Summarize the single most important focus for the next few days in one short Thai sentence.\n"]
    if prioritized:
        lines.append("Top prioritized actions:")
        for item in prioritized[:5]:
            lines.append(f"  - {item['title']} ({item['reason']}, priority {item['priority']})")
    if stale:
        lines.append("Stale projects to revisit:")
        for item in stale[:3]:
            lines.append(f"  - {item['title']} ({item['days_stale']} days stale)")

    prompt = "\n".join(lines)
    try:
        raw = await call_ollama(settings.LLM_PRIMARY, prompt=prompt, format=None, task="plan")
        text = (raw or "").strip()
        if text:
            return text
    except (httpx.TimeoutException, httpx.HTTPError, KeyError, TypeError) as e:
        logger.warning("Planner focus LLM failed: %s", e)

    return _fallback_focus(prioritized, stale)


# ── Activity patterns ───────────────────────────────────────────────────────


async def get_activity_patterns(days: int = 30) -> dict:
    if _using_pg():
        return await _activity_patterns_pg(days)
    return await _activity_patterns_sqlite(days)


async def _activity_patterns_pg(days: int) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=days)
    by_day = {name: 0 for name in _DAY_ORDER}
    by_hour = {str(h): 0 for h in range(24)}

    async with async_session_factory() as session:
        total = (await session.execute(
            select(PgHistory).where(PgHistory.timestamp >= cutoff)
        )).all()
        total_count = len(total)
        # Per-day-of-week (PostgreSQL: EXTRACT(DOW ...))
        from sqlalchemy import func as sa_func
        day_rows = (await session.execute(
            select(
                sa_func.extract("dow", PgHistory.timestamp).label("d"),
                sa_func.count().label("c"),
            )
            .where(PgHistory.timestamp >= cutoff)
            .group_by("d")
        )).all()
        hour_rows = (await session.execute(
            select(
                sa_func.extract("hour", PgHistory.timestamp).label("h"),
                sa_func.count().label("c"),
            )
            .where(PgHistory.timestamp >= cutoff)
            .group_by("h")
        )).all()
        # Top categories joined with notes — keep simple, count history entries
        # per category, fall back to a no-join 0 if join fails.
        try:
            category_rows = (await session.execute(
                select(PgNote.para_category, sa_func.count().label("c"))
                .join(PgHistory, PgHistory.note_id == PgNote.id)
                .where(PgHistory.timestamp >= cutoff)
                .group_by(PgNote.para_category)
                .order_by(sa_func.count().desc())
            )).all()
        except Exception:
            category_rows = []

    for row in day_rows:
        name = _DAY_NAMES.get(str(int(row.d)))
        if name:
            by_day[name] = int(row.c)
    for row in hour_rows:
        by_hour[str(int(row.h))] = int(row.c)

    most_active_day = max(by_day, key=by_day.get) if any(by_day.values()) else "mon"
    most_active_hour = int(max(by_hour, key=by_hour.get)) if any(by_hour.values()) else 0
    top_categories = [
        {"para_category": r.para_category, "count": int(r.c)} for r in category_rows
    ]
    return {
        "period_days": days,
        "total_actions": total_count,
        "by_day_of_week": by_day,
        "by_hour": by_hour,
        "most_active_day": most_active_day,
        "most_active_hour": most_active_hour,
        "top_categories": top_categories,
    }


async def _activity_patterns_sqlite(days: int) -> dict:
    from app.database import get_connection
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(sep=" ")
    by_day = {name: 0 for name in _DAY_ORDER}
    by_hour = {str(h): 0 for h in range(24)}

    async with get_connection() as db:
        total = (await (await db.execute(
            "SELECT COUNT(*) c FROM history WHERE timestamp >= ?", (cutoff,)
        )).fetchone())["c"]
        day_rows = await (await db.execute(
            """SELECT strftime('%w', timestamp) d, COUNT(*) c FROM history
               WHERE timestamp >= ? GROUP BY d""",
            (cutoff,),
        )).fetchall()
        hour_rows = await (await db.execute(
            """SELECT strftime('%H', timestamp) h, COUNT(*) c FROM history
               WHERE timestamp >= ? GROUP BY h""",
            (cutoff,),
        )).fetchall()
        category_rows = await (await db.execute(
            """SELECT n.para_category, COUNT(*) c FROM history h
               JOIN notes n ON n.id = h.note_id
               WHERE h.timestamp >= ?
               GROUP BY n.para_category ORDER BY c DESC""",
            (cutoff,),
        )).fetchall()

    for row in day_rows:
        name = _DAY_NAMES.get(row["d"])
        if name:
            by_day[name] = row["c"]
    for row in hour_rows:
        try:
            by_hour[str(int(row["h"]))] = row["c"]
        except (ValueError, TypeError):
            continue
    most_active_day = max(by_day, key=by_day.get) if any(by_day.values()) else "mon"
    most_active_hour = int(max(by_hour, key=by_hour.get)) if any(by_hour.values()) else 0
    top_categories = [{"para_category": row["para_category"], "count": row["c"]} for row in category_rows]
    return {
        "period_days": days,
        "total_actions": total,
        "by_day_of_week": by_day,
        "by_hour": by_hour,
        "most_active_day": most_active_day,
        "most_active_hour": most_active_hour,
        "top_categories": top_categories,
    }
