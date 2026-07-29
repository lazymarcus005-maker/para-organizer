"""SB-09 — Autonomous task generation and dispatch."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from app.config import settings
from app.database import get_connection
from app.notifier import notify_task_proposal, send_telegram, notification_chat_ids
from app.tasks import create_task

logger = logging.getLogger("para.autonomy")


async def _has_pending_task(db, note_id: int | None) -> bool:
    if note_id is None:
        return False
    row = await (await db.execute(
        "SELECT 1 FROM tasks WHERE note_id = ? AND status IN ('pending', 'dispatched') LIMIT 1",
        (note_id,),
    )).fetchone()
    return row is not None


async def generate_autonomous_tasks() -> dict:
    today = date.today()
    now = datetime.now()
    deadline_cutoff = (today + timedelta(days=3)).isoformat()
    stale_cutoff = (now - timedelta(days=settings.NOTIFY_STALE_DAYS)).isoformat(sep=" ")
    progress_stale_cutoff = (now - timedelta(days=7)).isoformat(sep=" ")

    proposals: list[dict] = []
    auto_dispatched = 0
    pending_approval = 0

    async with get_connection() as db:
        try:
            rows = await (await db.execute(
                """SELECT id, title, deadline FROM notes
                   WHERE status='active' AND deadline IS NOT NULL
                     AND deadline >= date('now') AND deadline <= ?""",
                (deadline_cutoff,),
            )).fetchall()
            for row in rows:
                try:
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
                    logger.exception("Failed to process deadline proposal for note %s", row["id"])
        except Exception:
            logger.exception("Failed to query deadline notes")

        try:
            rows = await (await db.execute(
                """SELECT id, title, updated_at FROM notes
                   WHERE para_category='projects' AND status='active' AND updated_at < ?""",
                (stale_cutoff,),
            )).fetchall()
            for row in rows:
                try:
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
                    logger.exception("Failed to process stale proposal for note %s", row["id"])
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
                try:
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
                    logger.exception("Failed to process progress proposal for note %s", row["id"])
        except Exception:
            logger.exception("Failed to query progress notes")

        level = settings.AUTONOMY_LEVEL
        for proposal in proposals:
            try:
                if level == "suggest_only":
                    await notify_task_proposal(proposal)
                    pending_approval += 1
                elif level == "auto_approve":
                    await create_task(db, proposal["note_id"], proposal["prompt"], proposal["task_type"])
                    for chat_id in notification_chat_ids():
                        await send_telegram(chat_id, f"🤖 สร้างงานอัตโนมัติ: {proposal['prompt']}")
                    auto_dispatched += 1
                elif level == "full_auto":
                    await create_task(db, proposal["note_id"], proposal["prompt"], proposal["task_type"])
                    auto_dispatched += 1
            except Exception:
                logger.exception("Failed to dispatch proposal for note %s", proposal["note_id"])

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
    async with get_connection() as db:
        return await create_task(db, note_id, prompt, task_type)
