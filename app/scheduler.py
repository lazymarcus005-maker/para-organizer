"""APScheduler setup and background maintenance jobs."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from apscheduler.events import EVENT_JOB_ERROR
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.classifier import classify_note, extract_deadline_from_text
from app.config import settings
from app.database import get_connection
from app.notifier import notify_deadline, notify_stale, send_digest, notify_escalation
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
            try:
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
            except Exception:
                logger.exception("Failed to reclassify note %s, skipping", note["id"])
                continue
        await db.commit()
    return changed


async def auto_archive_completed(now: datetime | None = None) -> int:
    timestamp = now or datetime.now()
    cutoff = timestamp - timedelta(days=settings.AUTO_ARCHIVE_DAYS)
    timestamp_str = timestamp.isoformat(sep=" ")
    archived = 0
    async with get_connection() as db:
        rows = await (await db.execute(
            """SELECT id, para_category FROM notes
               WHERE status = 'completed' AND updated_at < ?""",
            (cutoff.isoformat(sep=" "),),
        )).fetchall()
        for row in rows:
            try:
                await db.execute(
                    """UPDATE notes SET status='archived', para_category='archives',
                       archived_at=?, updated_at=? WHERE id=?""",
                    (timestamp_str, timestamp_str, row["id"]),
                )
                await db.execute(
                    """INSERT INTO history (note_id, action, old_value, new_value, reason)
                       VALUES (?, 'archived', ?, 'archives', 'auto_archive')""",
                    (row["id"], row["para_category"]),
                )
                archived += 1
            except Exception:
                logger.exception("Failed to auto-archive note %s, skipping", row["id"])
                continue
        await db.commit()
    return archived


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
            try:
                days_left = (date.fromisoformat(str(note["deadline"])[:10]) - today).days
            except ValueError:
                logger.warning("Note %s has malformed deadline %r, skipping", note["id"], note["deadline"])
                continue
            if days_left not in reminder_days:
                continue
            try:
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
            except Exception:
                logger.exception("Failed to process deadline notification for note %s", note["id"])
                continue
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
            try:
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
                        json.dumps({"updated_at": str(note["updated_at"])}, ensure_ascii=False),
                    ),
                )
                sent += int(success)
            except Exception:
                logger.exception("Failed to process stale notification for note %s", note["id"])
                continue
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


async def auto_escalate_urgent_notes(today: date | None = None) -> int:
    """Auto-escalate notes within 3 days of deadline from low/medium to high priority.
    
    Daily job that finds active notes where:
    - deadline <= today + 3 days
    - priority in ('low', 'medium')
    
    Escalates them to 'high' priority and sends notification.
    """
    today = today or date.today()
    deadline_cutoff = (today + timedelta(days=3)).isoformat()
    escalated = 0
    async with get_connection() as db:
        rows = await (await db.execute(
            """SELECT * FROM notes
               WHERE status='active' AND priority IN ('low', 'medium')
               AND deadline IS NOT NULL AND deadline <= ?
               ORDER BY deadline ASC""",
            (deadline_cutoff,),
        )).fetchall()
        for row in rows:
            note = row_to_note(row)
            try:
                # Calculate days remaining
                deadline_date = date.fromisoformat(str(note["deadline"])[:10])
                days_left = (deadline_date - today).days
                
                # Skip if deadline is in the past
                if days_left < 0:
                    continue
                
                old_priority = note["priority"]
                timestamp_str = datetime.now().isoformat(sep=" ")
                
                # Update priority to high
                await db.execute(
                    """UPDATE notes SET priority='high', updated_at=? WHERE id=?""",
                    (timestamp_str, note["id"]),
                )
                
                # Log history
                await db.execute(
                    """INSERT INTO history (note_id, action, old_value, new_value, reason)
                       VALUES (?, 'escalated', ?, 'high', ?)""",
                    (note["id"], old_priority, f"deadline in {days_left} days"),
                )
                
                # Send notification
                success = await notify_escalation(note, days_left, old_priority)
                
                # Log notification
                payload = json.dumps({
                    "old_priority": old_priority,
                    "days_left": days_left,
                }, ensure_ascii=False)
                await db.execute(
                    """INSERT INTO notifications
                       (note_id, type, status, scheduled_at, sent_at, payload)
                       VALUES (?, 'escalation', ?, ?, ?, ?)""",
                    (
                        note["id"], "sent" if success else "failed",
                        datetime.now().isoformat(),
                        datetime.now().isoformat() if success else None,
                        payload,
                    ),
                )
                escalated += 1
            except Exception:
                logger.exception("Failed to escalate note %s, skipping", note["id"])
                continue
        await db.commit()
    return escalated


def _log_job_error(event) -> None:
    """Log a scheduled job failure without affecting other jobs' schedules."""
    logger.error("Scheduled job %r failed: %s", event.job_id, event.exception)


def _parse_digest_time() -> tuple[int, int]:
    """Parse NOTIFY_DIGEST_TIME ('HH:MM') into an (hour, minute) tuple, defaulting to 08:00."""
    try:
        hour_str, minute_str = settings.NOTIFY_DIGEST_TIME.split(":", 1)
        return int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        logger.warning("Invalid NOTIFY_DIGEST_TIME=%r, defaulting to 08:00", settings.NOTIFY_DIGEST_TIME)
        return 8, 0


def reclassify_trigger() -> CronTrigger:
    """Build the reclassify cron trigger, falling back to the default interval if
    the configured value is missing/invalid (a persisted 0 or negative would make
    ``*/N`` raise and brick startup — see app.config._load_persisted_overrides)."""
    hours = settings.RECLASSIFY_INTERVAL_HOURS
    if not isinstance(hours, int) or hours < 1:
        logger.warning("Invalid RECLASSIFY_INTERVAL_HOURS=%r, defaulting to 6", hours)
        hours = 6
    return CronTrigger(hour=f"*/{hours}")


def digest_trigger() -> CronTrigger:
    """Build the weekly-digest cron trigger, falling back to 'mon' if the persisted
    NOTIFY_DIGEST_DAY is not a valid day-of-week expression (which would otherwise
    raise at CronTrigger construction and brick startup)."""
    hour, minute = _parse_digest_time()
    try:
        return CronTrigger(day_of_week=settings.NOTIFY_DIGEST_DAY, hour=hour, minute=minute)
    except (ValueError, TypeError):
        logger.warning("Invalid NOTIFY_DIGEST_DAY=%r, defaulting to 'mon'", settings.NOTIFY_DIGEST_DAY)
        return CronTrigger(day_of_week="mon", hour=hour, minute=minute)


scheduler = AsyncIOScheduler()
scheduler.add_listener(_log_job_error, EVENT_JOB_ERROR)
scheduler.add_job(
    reclassify_low_confidence_notes,
    reclassify_trigger(),
    id="reclassify", name="Reclassify low-confidence notes", replace_existing=True,
)
scheduler.add_job(
    auto_archive_completed, CronTrigger(hour=2, minute=0),
    id="auto_archive", name="Auto-archive completed notes", replace_existing=True,
)
scheduler.add_job(
    auto_escalate_urgent_notes, CronTrigger(hour=7, minute=0),
    id="escalate", name="Auto-escalate notes near deadline", replace_existing=True,
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
    send_weekly_digest, digest_trigger(),
    id="weekly_digest", name="Send weekly digest", replace_existing=True,
)
