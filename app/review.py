"""Weekly AI review — analyze the last 7 days of PARA activity and ask the LLM
to produce a markdown digest with insights and 3 concrete action recommendations.

`gather_review_data` collects the deterministic facts (counts, stale/neglected
items, completion rate) so they can be unit-tested and embedded in the prompt;
`generate_weekly_review` builds the prompt, calls the LLM (primary then fallback),
and falls back to a deterministic markdown summary if every model call fails so a
review is always produced.

Works against either backend (PostgreSQL in production, SQLite in local-dev).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import and_, func, select

from app.classifier import call_ollama
from app.config import settings
from app.database_v2 import async_session_factory
from app.models_v2 import Note as PgNote

logger = logging.getLogger("para.review")


def _using_pg() -> bool:
    return bool(settings.PARA_DB_URL and "postgresql" in settings.PARA_DB_URL)


async def gather_review_data(now: datetime | None = None) -> dict:
    """Collect the facts the review is built from: activity in the last 7 days,
    project/area status, stale (>= NOTIFY_STALE_DAYS with no update) items, and a
    completion rate. Uses its own connection so it's callable standalone."""
    if _using_pg():
        return await _gather_review_data_pg(now)
    return await _gather_review_data_sqlite(now)


async def _gather_review_data_pg(now: datetime | None) -> dict:
    now = now or datetime.utcnow()
    week_ago = now - timedelta(days=7)
    stale_dt = now - timedelta(days=settings.NOTIFY_STALE_DAYS)

    async with async_session_factory() as session:
        total = (await session.execute(
            select(func.count()).select_from(PgNote)
        )).scalar_one()

        category_rows = (await session.execute(
            select(PgNote.para_category, func.count().label("c"))
            .group_by(PgNote.para_category)
        )).all()
        status_rows = (await session.execute(
            select(PgNote.status, func.count().label("c"))
            .group_by(PgNote.status)
        )).all()
        active_projects = (await session.execute(
            select(PgNote.id, PgNote.title, PgNote.status, PgNote.deadline, PgNote.updated_at)
            .where(and_(PgNote.para_category == "projects", PgNote.status == "active"))
            .order_by(PgNote.deadline.is_(None), PgNote.deadline.asc())
        )).all()
        stale_projects = (await session.execute(
            select(PgNote.id, PgNote.title, PgNote.updated_at)
            .where(and_(
                PgNote.para_category == "projects",
                PgNote.status == "active",
                PgNote.updated_at < stale_dt,
            ))
            .order_by(PgNote.updated_at)
        )).all()
        neglected_areas = (await session.execute(
            select(PgNote.id, PgNote.title, PgNote.updated_at)
            .where(and_(
                PgNote.para_category == "areas",
                PgNote.status == "active",
                PgNote.updated_at < stale_dt,
            ))
            .order_by(PgNote.updated_at)
        )).all()
        completed_this_week = (await session.execute(
            select(PgNote.id, PgNote.title)
            .where(and_(
                PgNote.status.in_(("completed", "archived")),
                PgNote.updated_at >= week_ago,
            ))
        )).all()
        activity_rows = (await session.execute(
            select(PgNote.para_category, func.count().label("c"))
            .where(PgNote.updated_at >= week_ago)
            .group_by(PgNote.para_category)
            .order_by(func.count().desc())
        )).all()
        recent_resources = (await session.execute(
            select(PgNote.id, PgNote.title)
            .where(and_(
                PgNote.para_category == "resources",
                PgNote.created_at >= week_ago,
            ))
            .order_by(PgNote.created_at.desc())
            .limit(5)
        )).all()
        new_count = (await session.execute(
            select(func.count()).select_from(PgNote).where(PgNote.created_at >= week_ago)
        )).scalar_one()

    status_counts = {r.status: int(r.c) for r in status_rows}
    active_total = status_counts.get("active", 0)
    completed_total = status_counts.get("completed", 0) + status_counts.get("archived", 0)
    denominator = active_total + completed_total
    completion_rate = completed_total / denominator if denominator else 0.0
    activity = {r.para_category: int(r.c) for r in activity_rows}
    high_activity = activity_rows[0].para_category if activity_rows else None

    return {
        "total_notes": int(total),
        "by_category": {r.para_category: int(r.c) for r in category_rows},
        "status_counts": status_counts,
        "active_projects": [
            {"id": r.id, "title": r.title, "status": r.status,
             "deadline": r.deadline.isoformat() if r.deadline else None,
             "updated_at": r.updated_at.isoformat() if r.updated_at else None}
            for r in active_projects
        ],
        "stale_projects": [
            {"id": r.id, "title": r.title,
             "updated_at": r.updated_at.isoformat() if r.updated_at else None}
            for r in stale_projects
        ],
        "neglected_areas": [
            {"id": r.id, "title": r.title,
             "updated_at": r.updated_at.isoformat() if r.updated_at else None}
            for r in neglected_areas
        ],
        "completed_this_week": [{"id": r.id, "title": r.title} for r in completed_this_week],
        "activity_by_category": activity,
        "high_activity_category": high_activity,
        "recent_resources": [{"id": r.id, "title": r.title} for r in recent_resources],
        "new_notes_count": int(new_count),
        "completion_rate": completion_rate,
        "stale_days": settings.NOTIFY_STALE_DAYS,
    }


async def _gather_review_data_sqlite(now: datetime | None) -> dict:
    from app.database import get_connection
    now = now or datetime.now()
    week_ago = (now - timedelta(days=7)).isoformat(sep=" ")
    stale_cutoff = (now - timedelta(days=settings.NOTIFY_STALE_DAYS)).isoformat(sep=" ")

    async with get_connection() as db:
        total = (await (await db.execute("SELECT COUNT(*) c FROM notes")).fetchone())["c"]
        category_rows = await (await db.execute(
            "SELECT para_category, COUNT(*) c FROM notes GROUP BY para_category"
        )).fetchall()
        status_rows = await (await db.execute(
            "SELECT status, COUNT(*) c FROM notes GROUP BY status"
        )).fetchall()
        active_projects = await (await db.execute(
            """SELECT id, title, status, deadline, updated_at FROM notes
               WHERE para_category='projects' AND status='active'
               ORDER BY deadline IS NULL, deadline"""
        )).fetchall()
        stale_projects = await (await db.execute(
            """SELECT id, title, updated_at FROM notes
               WHERE para_category='projects' AND status='active' AND updated_at < ?
               ORDER BY updated_at""",
            (stale_cutoff,),
        )).fetchall()
        neglected_areas = await (await db.execute(
            """SELECT id, title, updated_at FROM notes
               WHERE para_category='areas' AND status='active' AND updated_at < ?
               ORDER BY updated_at""",
            (stale_cutoff,),
        )).fetchall()
        completed_this_week = await (await db.execute(
            """SELECT id, title FROM notes
               WHERE status IN ('completed', 'archived') AND updated_at >= ?""",
            (week_ago,),
        )).fetchall()
        activity_rows = await (await db.execute(
            """SELECT para_category, COUNT(*) c FROM notes
               WHERE updated_at >= ? GROUP BY para_category ORDER BY c DESC""",
            (week_ago,),
        )).fetchall()
        recent_resources = await (await db.execute(
            """SELECT id, title FROM notes
               WHERE para_category='resources' AND created_at >= ?
               ORDER BY created_at DESC LIMIT 5""",
            (week_ago,),
        )).fetchall()
        new_count = (await (await db.execute(
            "SELECT COUNT(*) c FROM notes WHERE created_at >= ?", (week_ago,)
        )).fetchone())["c"]

    status_counts = {row["status"]: row["c"] for row in status_rows}
    active_total = status_counts.get("active", 0)
    completed_total = status_counts.get("completed", 0) + status_counts.get("archived", 0)
    denominator = active_total + completed_total
    completion_rate = completed_total / denominator if denominator else 0.0
    activity = {row["para_category"]: row["c"] for row in activity_rows}
    high_activity = activity_rows[0]["para_category"] if activity_rows else None

    return {
        "total_notes": total,
        "by_category": {row["para_category"]: row["c"] for row in category_rows},
        "status_counts": status_counts,
        "active_projects": [dict(row) for row in active_projects],
        "stale_projects": [dict(row) for row in stale_projects],
        "neglected_areas": [dict(row) for row in neglected_areas],
        "completed_this_week": [dict(row) for row in completed_this_week],
        "activity_by_category": activity,
        "high_activity_category": high_activity,
        "recent_resources": [dict(row) for row in recent_resources],
        "new_notes_count": new_count,
        "completion_rate": completion_rate,
        "stale_days": settings.NOTIFY_STALE_DAYS,
    }


REVIEW_SYSTEM = (
    "You are a productivity coach reviewing a user's PARA second-brain notes. "
    "Write a concise weekly review in markdown (Thai mixed with English is fine). "
    "Analyze the data, call out stale projects and neglected areas, report the "
    "completion rate, then finish with a section '## 3 Action Recommendations' "
    "listing exactly 3 specific, prioritized actions (prioritize / refocus / archive)."
)


def _build_prompt(data: dict) -> str:
    """Render the gathered facts into a structured LLM prompt."""
    lines: list[str] = ["Here is this week's PARA activity data:\n"]

    cats = data["by_category"]
    lines.append(
        "Category totals — "
        f"projects: {cats.get('projects', 0)}, areas: {cats.get('areas', 0)}, "
        f"resources: {cats.get('resources', 0)}, archives: {cats.get('archives', 0)}, "
        f"inbox: {cats.get('inbox', 0)}"
    )
    lines.append(
        f"Completion rate: {data['completion_rate'] * 100:.0f}% "
        f"(completed vs active)"
    )
    lines.append(f"New notes this week: {data['new_notes_count']}")
    lines.append(f"Completed/archived this week: {len(data['completed_this_week'])}")
    if data["high_activity_category"]:
        lines.append(f"Highest-activity category this week: {data['high_activity_category']}")

    lines.append("\nActive projects (title — status — deadline):")
    if data["active_projects"]:
        for p in data["active_projects"]:
            lines.append(f"  - {p['title']} — {p['status']} — deadline {p.get('deadline') or 'none'}")
    else:
        lines.append("  - (none)")

    lines.append(f"\nStale projects (no update in >= {data['stale_days']} days):")
    if data["stale_projects"]:
        for p in data["stale_projects"]:
            lines.append(f"  - {p['title']} (last updated {p.get('updated_at')})")
    else:
        lines.append("  - (none)")

    lines.append(f"\nNeglected areas (no update in >= {data['stale_days']} days):")
    if data["neglected_areas"]:
        for a in data["neglected_areas"]:
            lines.append(f"  - {a['title']} (last updated {a.get('updated_at')})")
    else:
        lines.append("  - (none)")

    if data["recent_resources"]:
        lines.append("\nNew resources this week:")
        for r in data["recent_resources"]:
            lines.append(f"  - {r['title']}")

    lines.append(
        "\nWrite the weekly review now. Highlight the stale projects and neglected "
        "areas above, state the completion rate, and end with '## 3 Action "
        "Recommendations' containing exactly 3 prioritized actions."
    )
    return "\n".join(lines)


def _fallback_markdown(data: dict) -> str:
    """Deterministic markdown used when every LLM call fails, so a review is
    always returned and still contains stale detection + 3 action items."""
    lines = ["# 🧠 PARA Weekly Review", ""]
    lines.append(f"- Total notes: {data['total_notes']}")
    lines.append(f"- New this week: {data['new_notes_count']}")
    lines.append(f"- Completion rate: {data['completion_rate'] * 100:.0f}%")
    lines.append("")
    lines.append(f"## ⚠️ Stale projects (>= {data['stale_days']} days without update)")
    if data["stale_projects"]:
        lines.extend(f"- {p['title']}" for p in data["stale_projects"])
    else:
        lines.append("- None 🎉")
    lines.append("")
    lines.append(f"## 💤 Neglected areas (>= {data['stale_days']} days without update)")
    if data["neglected_areas"]:
        lines.extend(f"- {a['title']}" for a in data["neglected_areas"])
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## 3 Action Recommendations")
    if data["stale_projects"]:
        lines.append(f"1. Prioritize: revisit stale project \"{data['stale_projects'][0]['title']}\".")
    else:
        lines.append("1. Prioritize: pick the next active project to push forward.")
    if data["neglected_areas"]:
        lines.append(f"2. Refocus: schedule time for neglected area \"{data['neglected_areas'][0]['title']}\".")
    else:
        lines.append("2. Refocus: review your areas of responsibility for balance.")
    lines.append("3. Archive: move completed or no-longer-relevant notes to archives.")
    return "\n".join(lines)


async def generate_weekly_review(now: datetime | None = None) -> str:
    """Analyze the last 7 days of notes and return a markdown review.

    Calls the LLM (primary then fallback); if every attempt fails, returns a
    deterministic fallback so a review is always produced. Never raises.
    """
    data = await gather_review_data(now)
    prompt = _build_prompt(data)

    for model in [settings.LLM_PRIMARY, settings.LLM_FALLBACK]:
        try:
            raw = await call_ollama(
                model,
                messages=[
                    {"role": "system", "content": REVIEW_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                format=None,
                task="review",
            )
            text = (raw or "").strip()
            if text:
                return text
        except (httpx.TimeoutException, httpx.HTTPError, KeyError, TypeError) as e:
            logger.warning("Weekly review LLM %s failed: %s", model, e)
            continue

    logger.warning("All LLM models failed for weekly review, using fallback markdown")
    return _fallback_markdown(data)
