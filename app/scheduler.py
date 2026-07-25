"""APScheduler setup and background maintenance jobs."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.classifier import classify_note, extract_deadline_from_text
from app.config import settings
from app.database import get_connection
from app.notifier import notify_deadline, notify_stale, send_digest
from app.utils import row_to_note

logger = logging.getLogger("para.scheduler")


async def reclassify_low_confidence_notes() -> int:
    async with get_connection() as db:
        rows = await (await db.execute(
            """SELECT * FROM notes WHERE status = 'active' AND llm_confidence < ?""",
            (settings.RECLASSIFY_CONFIDENCE_THRESHOLD,),
        )).fetchall()
        changed = 0
        for row in rows:
            note = row_to_note(row)
            result = await classify_note(note["title"], note["content"])
            deadline = result.get("deadline")
            if not deadline:
                extracted = extract_deadline_from_text(note["content"])
                deadline = extracted.isoformat() if extracted else None
            await db.execute(
                """UPDATE notes SET para_category=?, sub_category=?, priority=?, deadline=?,
                   tags=?, llm_model=?, llm_confidence=?, llm_reasoning=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    result.get("para_category", "inbox"), result.get("sub_category"),
                    result.get("priority", "medium"), deadline,
                    json.dumps(result.get("tags", []), ensure_ascii=False),
                    result.get("llm_model"), float(result.get("confidence", 0.0)),
                    result.get("reasoning"), note["id"],
                ),
            )
            await db.execute(
                """INSERT INTO history (note_id, action, old_value, new_value, reason)
                   VALUES (?, 'classified', ?, ?, ?)""",
                (note["id"], note["para_category"], result.get("para_category"), result.get("reasoning")),
            )
            changed += 1
        await db.commit()
    return changed


async def auto_archive_completed(now: datetime | None = None) -> int:
    cutoff = (now or datetime.now()) - timedelta(days=settings.AUTO_ARCHIVE_DAYS)
    async with get_connection() as db:
        rows = await (await db.execute(
            """SELECT id, para_category FROM notes
               WHERE status = 'completed' AND updated_at < ?""",
            (cutoff.isoformat(sep=" "),),
        )).fetchall()
        for row in rows:
            await db.execute(
                """UPDATE notes SET status='archived', para_category='archives',
                   archived_at=?, updated_at=? WHERE id=?""",
                ((now or datetime.now()).isoformat(sep=" "), (now or datetime.now()).isoformat(sep=" "), row["id"]),
            )
            await db.execute(
                """INSERT INTO history (note_id, action, old_value, new_value, reason)
                   VALUES (?, 'archived', ?, 'archives', 'auto_archive')""",
                (row["id"], row["para_category"]),
            )
        await db.commit()
    return len(rows)


async def check_deadlines_and_notify(today: date | None = None) -> int:
    today = today or date.today()
    reminder_days = {
        int(value.strip()) for value in settings.NOTIFY_DEADLINE_DAYS.split(",")
        if value.strip().isdigit()
    }
    async with get_connection() as db:
        rows = await (await db.execute(
            "SELECT * FROM notes WHERE status='active' AND deadline IS NOT NULL"
        )).fetchall()
        sent = 0
        for row in rows:
            note = row_to_note(row)
            days_left = (date.fromisoformat(str(note["deadline"])[:10]) - today).days
            if days_left not in reminder_days:
                continue
            payload = json.dumps({"days_left": days_left}, ensure_ascii=False)
            duplicate = await (await db.execute(
                """SELECT 1 FROM notifications WHERE note_id=? AND type='deadline'
                   AND date(scheduled_at)=? AND payload=? LIMIT 1""",
                (note["id"], today.isoformat(), payload),
            )).fetchone()
            if duplicate:
                continue
            success = await notify_deadline(note, days_left)
            await db.execute(
                """INSERT INTO notifications
                   (note_id, type, status, scheduled_at, sent_at, payload)
                   VALUES (?, 'deadline', ?, ?, ?, ?)""",
                (
                    note["id"], "sent" if success else "failed",
                    datetime.combine(today, datetime.min.time()).isoformat(),
                    datetime.now().isoformat() if success else None, payload,
                ),
            )
            if success:
                await db.execute(
                    """INSERT INTO history (note_id, action, new_value)
                       VALUES (?, 'deadline_reminded', ?)""",
                    (note["id"], str(days_left)),
                )
                sent += 1
        await db.commit()
    return sent


async def check_stale_projects(now: datetime | None = None) -> int:
    now = now or datetime.now()
    cutoff = now - timedelta(days=settings.NOTIFY_STALE_DAYS)
    async with get_connection() as db:
        rows = await (await db.execute(
            """SELECT * FROM notes WHERE para_category='projects' AND status='active'
               AND updated_at < ?""",
            (cutoff.isoformat(sep=" "),),
        )).fetchall()
        sent = 0
        for row in rows:
            note = row_to_note(row)
            duplicate = await (await db.execute(
                """SELECT 1 FROM notifications WHERE note_id=? AND type='stale'
                   AND date(scheduled_at)=? LIMIT 1""",
                (note["id"], now.date().isoformat()),
            )).fetchone()
            if duplicate:
                continue
            success = await notify_stale(note)
            await db.execute(
                """INSERT INTO notifications
                   (note_id, type, status, scheduled_at, sent_at, payload)
                   VALUES (?, 'stale', ?, ?, ?, ?)""",
                (
                    note["id"], "sent" if success else "failed", now.isoformat(),
                    now.isoformat() if success else None,
                    json.dumps({"updated_at": str(note["updated_at"])}),
                ),
            )
            sent += int(success)
        await db.commit()
    return sent


async def build_digest(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    week_ago = now - timedelta(days=7)
    stale_cutoff = now - timedelta(days=settings.NOTIFY_STALE_DAYS)
    async with get_connection() as db:
        total = (await (await db.execute("SELECT COUNT(*) c FROM notes")).fetchone())["c"]
        category_rows = await (await db.execute(
            "SELECT para_category, COUNT(*) c FROM notes GROUP BY para_category"
        )).fetchall()
        completed = await (await db.execute(
            """SELECT id, title FROM notes WHERE status='archived' AND archived_at >= ?""",
            (week_ago.isoformat(sep=" "),),
        )).fetchall()
        active = await (await db.execute(
            """SELECT id, title, deadline FROM notes
               WHERE para_category='projects' AND status='active' ORDER BY deadline"""
        )).fetchall()
        stale = await (await db.execute(
            """SELECT id, title FROM notes WHERE para_category='projects'
               AND status='active' AND updated_at < ?""",
            (stale_cutoff.isoformat(sep=" "),),
        )).fetchall()
        new_count = (await (await db.execute(
            "SELECT COUNT(*) c FROM notes WHERE created_at >= ?",
            (week_ago.isoformat(sep=" "),),
        )).fetchone())["c"]
    return {
        "total_notes": total,
        "by_category": {row["para_category"]: row["c"] for row in category_rows},
        "completed_this_week": [dict(row) for row in completed],
        "active_projects": [dict(row) for row in active],
        "stale_projects": [dict(row) for row in stale],
        "new_notes_count": new_count,
    }


async def send_weekly_digest(now: datetime | None = None) -> bool:
    data = await build_digest(now)
    success = await send_digest(data)
    async with get_connection() as db:
        timestamp = now or datetime.now()
        await db.execute(
            """INSERT INTO notifications (type, status, scheduled_at, sent_at, payload)
               VALUES ('digest', ?, ?, ?, ?)""",
            (
                "sent" if success else "failed", timestamp.isoformat(),
                timestamp.isoformat() if success else None,
                json.dumps(data, default=str, ensure_ascii=False),
            ),
        )
        await db.commit()
    return success


# Names used in the delivery plan, kept as job-callable aliases.
reclassify_job = reclassify_low_confidence_notes
auto_archive_job = auto_archive_completed
deadline_check_job = check_deadlines_and_notify
stale_project_job = check_stale_projects
weekly_digest_job = send_weekly_digest

scheduler = AsyncIOScheduler()
scheduler.add_job(
    reclassify_low_confidence_notes,
    CronTrigger(hour=f"*/{settings.RECLASSIFY_INTERVAL_HOURS}"),
    id="reclassify", name="Reclassify low-confidence notes", replace_existing=True,
)
scheduler.add_job(
    auto_archive_completed, CronTrigger(hour=2, minute=0),
    id="auto_archive", name="Auto-archive completed notes", replace_existing=True,
)
scheduler.add_job(
    check_deadlines_and_notify, CronTrigger(hour=9, minute=0),
    id="deadline_check", name="Check deadlines and send notifications", replace_existing=True,
)
scheduler.add_job(
    check_stale_projects, CronTrigger(hour=18, minute=0),
    id="stale_check", name="Check stale projects", replace_existing=True,
)
scheduler.add_job(
    send_weekly_digest, CronTrigger(day_of_week=settings.NOTIFY_DIGEST_DAY, hour=8, minute=0),
    id="weekly_digest", name="Send weekly digest", replace_existing=True,
)
