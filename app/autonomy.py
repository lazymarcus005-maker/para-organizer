"""SB-09 — Autonomous task generation and dispatch.

Works against either backend (PostgreSQL in production, SQLite in local-dev /
legacy mode). Auto-detected by the configured ``PARA_DB_URL``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import and_, select

from app.config import settings
from app.database_v2 import async_session_factory
from app.models_v2 import Note as PgNote
from app.models_v2 import Task as PgTask
from app.notifier import notify_task_proposal, send_telegram, notification_chat_ids
from app.tasks import create_task

logger = logging.getLogger("para.autonomy")


def _using_pg() -> bool:
    return bool(settings.PARA_DB_URL and "postgresql" in settings.PARA_DB_URL)


async def _has_pending_task(db_or_session, note_id: int | None) -> bool:
    if note_id is None:
        return False
    is_pg = hasattr(db_or_session, "add") and hasattr(db_or_session, "flush") and not hasattr(db_or_session, "execute_fetchall")
    if is_pg:
        row = (await db_or_session.execute(
            select(PgTask.id)
            .where(and_(PgTask.note_id == note_id, PgTask.status.in_(("pending", "dispatched"))))
            .limit(1)
        )).first()
        return row is not None
    row = await (await db_or_session.execute(
        "SELECT 1 FROM tasks WHERE note_id = ? AND status IN ('pending', 'dispatched') LIMIT 1",
        (note_id,),
    )).fetchone()
    return row is not None


# ── Per-backend proposal collection ─────────────────────────────────────────


async def _collect_proposals_pg(session) -> list[dict]:
    proposals: list[dict] = []
    today = date.today()
    now = datetime.utcnow()
    deadline_cutoff = (today + timedelta(days=3)).isoformat()
    stale_cutoff = now - timedelta(days=settings.NOTIFY_STALE_DAYS)

    # Deadline-driven
    try:
        rows = (await session.execute(
            select(PgNote.id, PgNote.title, PgNote.deadline)
            .where(and_(
                PgNote.status == "active",
                PgNote.deadline.isnot(None),
                PgNote.deadline >= today,
                PgNote.deadline <= date.fromisoformat(deadline_cutoff),
            ))
        )).all()
        for row in rows:
            if await _has_pending_task(session, row.id):
                continue
            proposals.append({
                "note_id": row.id,
                "prompt": f"เตรียม {row.title} ก่อน deadline",
                "task_type": "general",
                "reason": f"deadline {row.deadline}",
                "confidence": 0.9,
            })
    except Exception:
        logger.exception("Failed to query deadline notes (PG)")

    # Stale
    try:
        rows = (await session.execute(
            select(PgNote.id, PgNote.title, PgNote.updated_at)
            .where(and_(
                PgNote.para_category == "projects",
                PgNote.status == "active",
                PgNote.updated_at < stale_cutoff,
            ))
        )).all()
        for row in rows:
            if await _has_pending_task(session, row.id):
                continue
            updated = row.updated_at
            days_stale = (now - updated).days if updated else settings.NOTIFY_STALE_DAYS
            proposals.append({
                "note_id": row.id,
                "prompt": f"ทบทวน {row.title} — ไม่อัปเดต {days_stale} วัน",
                "task_type": "review",
                "reason": f"stale {days_stale} days",
                "confidence": 0.7,
            })
    except Exception:
        logger.exception("Failed to query stale notes (PG)")

    return proposals


async def _collect_proposals_sqlite(db) -> list[dict]:
    proposals: list[dict] = []
    today = date.today()
    now = datetime.now()
    deadline_cutoff = (today + timedelta(days=3)).isoformat()
    stale_cutoff = (now - timedelta(days=settings.NOTIFY_STALE_DAYS)).isoformat(sep=" ")
    progress_stale_cutoff = (now - timedelta(days=7)).isoformat(sep=" ")

    try:
        rows = await (await db.execute(
            """SELECT id, title, deadline FROM notes
               WHERE status='active' AND deadline IS NOT NULL
                 AND deadline >= date('now') AND deadline <= ?""",
            (deadline_cutoff,),
        )).fetchall()
        for row in rows:
            if await _has_pending_task(db, row["id"]):
                continue
            proposals.append({
                "note_id": row["id"],
                "prompt": f"เตรียม {row['title']} ก่อน deadline",
                "task_type": "general",
                "reason": f"deadline {row['deadline']}",
                "confidence": 0.9,
            })
    except Exception:
        logger.exception("Failed to query deadline notes")

    try:
        rows = await (await db.execute(
            """SELECT id, title, updated_at FROM notes
               WHERE para_category='projects' AND status='active' AND updated_at < ?""",
            (stale_cutoff,),
        )).fetchall()
        for row in rows:
            if await _has_pending_task(db, row["id"]):
                continue
            updated = datetime.fromisoformat(str(row["updated_at"]))
            days_stale = (now - updated).days
            proposals.append({
                "note_id": row["id"],
                "prompt": f"ทบทวน {row['title']} — ไม่อัปเดต {days_stale} วัน",
                "task_type": "review",
                "reason": f"stale {days_stale} days",
                "confidence": 0.7,
            })
    except Exception:
        logger.exception("Failed to query stale notes")

    try:
        rows = await (await db.execute(
            """SELECT DISTINCT n.id, n.title, n.progress, n.updated_at
               FROM notes n
               JOIN action_items ai ON ai.note_id = n.id
               WHERE n.status='active' AND n.progress IS NOT NULL
                 AND n.progress > 0 AND n.progress < 100
                 AND n.updated_at < ?""",
            (progress_stale_cutoff,),
        )).fetchall()
        for row in rows:
            if await _has_pending_task(db, row["id"]):
                continue
            progress = int(row["progress"])
            proposals.append({
                "note_id": row["id"],
                "prompt": f"ทำงาน {row['title']} ต่อ (progress {progress}%)",
                "task_type": "general",
                "reason": f"incomplete at {progress}%, stale > 7 days",
                "confidence": 0.6,
            })
    except Exception:
        logger.exception("Failed to query progress notes")

    return proposals


# ── Public entry point ──────────────────────────────────────────────────────


async def generate_autonomous_tasks() -> dict:
    proposals: list[dict] = []
    auto_dispatched = 0
    pending_approval = 0
    level = settings.AUTONOMY_LEVEL

    if _using_pg():
        async with async_session_factory() as session:
            proposals = await _collect_proposals_pg(session)
            for proposal in proposals:
                try:
                    if level == "suggest_only":
                        await notify_task_proposal(proposal)
                        pending_approval += 1
                    elif level == "auto_approve":
                        await create_task(
                            session, proposal["note_id"],
                            proposal["prompt"], proposal["task_type"],
                        )
                        for chat_id in notification_chat_ids():
                            await send_telegram(
                                chat_id,
                                f"🤖 สร้างงานอัตโนมัติ: {proposal['prompt']}",
                            )
                        auto_dispatched += 1
                    elif level == "full_auto":
                        await create_task(
                            session, proposal["note_id"],
                            proposal["prompt"], proposal["task_type"],
                        )
                        auto_dispatched += 1
                except Exception:
                    logger.exception(
                        "Failed to dispatch proposal for note %s",
                        proposal["note_id"],
                    )
            await session.commit()
    else:
        from app.database import get_connection
        async with get_connection() as db:
            proposals = await _collect_proposals_sqlite(db)
            for proposal in proposals:
                try:
                    if level == "suggest_only":
                        await notify_task_proposal(proposal)
                        pending_approval += 1
                    elif level == "auto_approve":
                        await create_task(
                            db, proposal["note_id"],
                            proposal["prompt"], proposal["task_type"],
                        )
                        for chat_id in notification_chat_ids():
                            await send_telegram(
                                chat_id,
                                f"🤖 สร้างงานอัตโนมัติ: {proposal['prompt']}",
                            )
                        auto_dispatched += 1
                    elif level == "full_auto":
                        await create_task(
                            db, proposal["note_id"],
                            proposal["prompt"], proposal["task_type"],
                        )
                        auto_dispatched += 1
                except Exception:
                    logger.exception(
                        "Failed to dispatch proposal for note %s",
                        proposal["note_id"],
                    )
            await db.commit()

    logger.info(
        "Autonomous task generation: %d proposals, %d auto-dispatched, %d pending approval",
        len(proposals), auto_dispatched, pending_approval,
    )
    return {
        "proposals": proposals,
        "auto_dispatched": auto_dispatched,
        "pending_approval": pending_approval,
    }


async def handle_autonomy_approval(
    note_id: int | None, prompt: str, task_type: str, approved: bool
) -> dict | None:
    if not approved:
        return None
    if _using_pg():
        async with async_session_factory() as session:
            result = await create_task(session, note_id, prompt, task_type)
            await session.commit()
            return result
    from app.database import get_connection
    async with get_connection() as db:
        return await create_task(db, note_id, prompt, task_type)
