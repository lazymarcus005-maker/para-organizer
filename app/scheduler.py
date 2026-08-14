"""APScheduler setup and background maintenance jobs.

Every job is dual-backend aware: it uses PostgreSQL / SQLAlchemy when
``settings.PARA_DB_URL`` points at a Postgres instance (the production case on
Dokploy) and falls back to the legacy aiosqlite path when running local-dev
without a Postgres URL. The 9 jobs themselves are unchanged — only the storage
plumbing behind them.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from apscheduler.events import EVENT_JOB_ERROR
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import and_, select

from app.classifier import classify_note, extract_deadline_from_text
from app.config import settings
from app.database_v2 import async_session_factory
from app.embed import embed_text
from app.events import emit_event
from app.models_v2 import History as PgHistory
from app.models_v2 import Note as PgNote
from app.models_v2 import Notification as PgNotification
from app.autonomy import generate_autonomous_tasks
from app.notifier import notify_deadline, notify_stale, send_digest, send_review, notify_escalation
from app.review import generate_weekly_review
from app.utils import row_to_note
from app.vector_store import index_note

logger = logging.getLogger("para.scheduler")


def _using_pg() -> bool:
    return bool(settings.PARA_DB_URL and "postgresql" in settings.PARA_DB_URL)


# ── Reclassify ─────────────────────────────────────────────────────────────


async def reclassify_low_confidence_notes() -> int:
    if _using_pg():
        return await _reclassify_pg()
    return await _reclassify_sqlite()


async def _reclassify_pg() -> int:
    threshold = settings.RECLASSIFY_CONFIDENCE_THRESHOLD
    changed = 0
    async with async_session_factory() as session:
        rows = (await session.execute(
            select(PgNote)
            .where(and_(PgNote.status == "active", PgNote.llm_confidence < threshold))
        )).scalars().all()
        for note in rows:
            try:
                result = await classify_note(note.title, note.content)
                deadline_raw = result.get("deadline")
                if not deadline_raw:
                    extracted = extract_deadline_from_text(note.content or "")
                    deadline_raw = extracted.isoformat() if extracted else None
                old_category = note.para_category
                note.para_category = result.get("para_category", note.para_category)
                note.sub_category = result.get("sub_category", note.sub_category)
                note.priority = result.get("priority", note.priority)
                if deadline_raw:
                    try:
                        note.deadline = date.fromisoformat(str(deadline_raw)[:10])
                    except (ValueError, TypeError):
                        pass
                note.tags = result.get("tags", note.tags or [])
                note.llm_model = result.get("llm_model", note.llm_model)
                note.llm_confidence = float(result.get("confidence", 0.0))
                note.llm_reasoning = result.get("reasoning", note.llm_reasoning)
                session.add(PgHistory(
                    note_id=note.id, action="classified",
                    old_value=old_category, new_value=note.para_category,
                    reason=note.llm_reasoning,
                ))
                changed += 1
            except Exception:
                logger.exception("Failed to reclassify note %s, skipping", note.id)
                continue
        await session.commit()
    return changed


async def _reclassify_sqlite() -> int:
    from app.database import get_connection
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


# ── Auto-archive ───────────────────────────────────────────────────────────


async def auto_archive_completed(now: datetime | None = None) -> int:
    if _using_pg():
        return await _auto_archive_pg(now)
    return await _auto_archive_sqlite(now)


async def _auto_archive_pg(now: datetime | None) -> int:
    timestamp = (now or datetime.utcnow()).isoformat()
    cutoff = (now or datetime.utcnow()) - timedelta(days=settings.AUTO_ARCHIVE_DAYS)
    archived = 0
    async with async_session_factory() as session:
        rows = (await session.execute(
            select(PgNote.id, PgNote.para_category)
            .where(and_(PgNote.status == "completed", PgNote.updated_at < cutoff))
        )).all()
        for row in rows:
            try:
                note = await session.get(PgNote, row.id)
                if note is None:
                    continue
                old = note.para_category
                note.status = "archived"
                note.para_category = "archives"
                note.archived_at = datetime.utcnow()
                note.updated_at = datetime.utcnow()
                session.add(PgHistory(
                    note_id=note.id, action="archived",
                    old_value=old, new_value="archives", reason="auto_archive",
                ))
                archived += 1
            except Exception:
                logger.exception("Failed to auto-archive note %s, skipping", row.id)
                continue
        await session.commit()
    return archived


async def _auto_archive_sqlite(now: datetime | None) -> int:
    from app.database import get_connection
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


# ── Deadlines notify ───────────────────────────────────────────────────────


async def check_deadlines_and_notify(today: date | None = None) -> int:
    if _using_pg():
        return await _check_deadlines_pg(today)
    return await _check_deadlines_sqlite(today)


async def _check_deadlines_pg(today: date | None) -> int:
    today = today or date.today()
    reminder_days = {
        int(value.strip()) for value in settings.NOTIFY_DEADLINE_DAYS.split(",")
        if value.strip().isdigit()
    }
    sent = 0
    async with async_session_factory() as session:
        rows = (await session.execute(
            select(PgNote).where(and_(PgNote.status == "active", PgNote.deadline.isnot(None)))
        )).scalars().all()
        for note in rows:
            try:
                days_left = (note.deadline - today).days
            except (TypeError, AttributeError):
                continue
            if days_left not in reminder_days:
                continue
            note_dict = {
                "id": note.id, "title": note.title,
                "deadline": note.deadline.isoformat() if note.deadline else None,
                "priority": note.priority,
            }
            try:
                # Dedup: a 'deadline' notification already exists for this note + day + days_left
                payload = json.dumps({"days_left": days_left}, ensure_ascii=False)
                from app.models_v2 import Notification as PgNotif
                dup = (await session.execute(
                    select(PgNotif.id)
                    .where(and_(
                        PgNotif.note_id == note.id,
                        PgNotif.type == "deadline",
                        PgNotif.payload == payload,
                    ))
                    .limit(1)
                )).first()
                if dup:
                    continue
                success = await notify_deadline(note_dict, days_left)
                session.add(PgNotification(
                    note_id=note.id, type="deadline",
                    status="sent" if success else "failed",
                    scheduled_at=datetime.utcnow(),
                    sent_at=datetime.utcnow() if success else None,
                    payload=payload,
                ))
                if success:
                    session.add(PgHistory(
                        note_id=note.id, action="deadline_reminded",
                        new_value=str(days_left),
                    ))
                    sent += 1
                    await emit_event(session, "note.deadline_approaching", note.id, {
                        "days_left": days_left,
                        "deadline": str(note.deadline),
                    })
            except Exception:
                logger.exception("Failed to process deadline notification for note %s", note.id)
                continue
        await session.commit()
    return sent


async def _check_deadlines_sqlite(today: date | None) -> int:
    from app.database import get_connection
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
                    await emit_event(db, "note.deadline_approaching", note["id"], {
                        "days_left": days_left,
                        "deadline": str(note["deadline"]),
                    })
            except Exception:
                logger.exception("Failed to process deadline notification for note %s", note["id"])
                continue
        await db.commit()
    return sent


# ── Stale projects ─────────────────────────────────────────────────────────


async def check_stale_projects(now: datetime | None = None) -> int:
    if _using_pg():
        return await _check_stale_pg(now)
    return await _check_stale_sqlite(now)


async def _check_stale_pg(now: datetime | None) -> int:
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=settings.NOTIFY_STALE_DAYS)
    sent = 0
    async with async_session_factory() as session:
        rows = (await session.execute(
            select(PgNote).where(and_(
                PgNote.para_category == "projects",
                PgNote.status == "active",
                PgNote.updated_at < cutoff,
            ))
        )).scalars().all()
        for note in rows:
            try:
                note_dict = {
                    "id": note.id, "title": note.title,
                    "updated_at": note.updated_at.isoformat() if note.updated_at else None,
                }
                # Dedup by note + day
                from app.models_v2 import Notification as PgNotif
                dup = (await session.execute(
                    select(PgNotif.id)
                    .where(and_(
                        PgNotif.note_id == note.id,
                        PgNotif.type == "stale",
                        PgNotif.scheduled_at >= now.replace(hour=0, minute=0, second=0, microsecond=0),
                    ))
                    .limit(1)
                )).first()
                if dup:
                    continue
                success = await notify_stale(note_dict)
                session.add(PgNotification(
                    note_id=note.id, type="stale",
                    status="sent" if success else "failed",
                    scheduled_at=now,
                    sent_at=now if success else None,
                    payload=json.dumps({"updated_at": str(note.updated_at)}, ensure_ascii=False),
                ))
                sent += int(success)
                if success:
                    await emit_event(session, "note.stale", note.id, {
                        "updated_at": str(note.updated_at),
                    })
            except Exception:
                logger.exception("Failed to process stale notification for note %s", note.id)
                continue
        await session.commit()
    return sent


async def _check_stale_sqlite(now: datetime | None) -> int:
    from app.database import get_connection
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
                if success:
                    await emit_event(db, "note.stale", note["id"], {
                        "updated_at": str(note["updated_at"]),
                    })
            except Exception:
                logger.exception("Failed to process stale notification for note %s", note["id"])
                continue
        await db.commit()
    return sent


# ── Digest ────────────────────────────────────────────────────────────────


async def build_digest(now: datetime | None = None) -> dict:
    if _using_pg():
        return await _build_digest_pg(now)
    return await _build_digest_sqlite(now)


async def _build_digest_pg(now: datetime | None) -> dict:
    now = now or datetime.utcnow()
    week_ago = now - timedelta(days=7)
    stale_cutoff = now - timedelta(days=settings.NOTIFY_STALE_DAYS)
    async with async_session_factory() as session:
        from sqlalchemy import func as sa_func
        total = (await session.execute(select(sa_func.count()).select_from(PgNote))).scalar_one()
        category_rows = (await session.execute(
            select(PgNote.para_category, sa_func.count().label("c"))
            .group_by(PgNote.para_category)
        )).all()
        completed = (await session.execute(
            select(PgNote.id, PgNote.title)
            .where(and_(PgNote.status == "archived", PgNote.archived_at >= week_ago))
        )).all()
        active = (await session.execute(
            select(PgNote.id, PgNote.title, PgNote.deadline)
            .where(and_(PgNote.para_category == "projects", PgNote.status == "active"))
            .order_by(PgNote.deadline.asc())
        )).all()
        stale = (await session.execute(
            select(PgNote.id, PgNote.title)
            .where(and_(
                PgNote.para_category == "projects",
                PgNote.status == "active",
                PgNote.updated_at < stale_cutoff,
            ))
        )).all()
        new_count = (await session.execute(
            select(sa_func.count()).select_from(PgNote).where(PgNote.created_at >= week_ago)
        )).scalar_one()

    return {
        "total_notes": int(total or 0),
        "by_category": {r.para_category: int(r.c) for r in category_rows},
        "completed_this_week": [{"id": r.id, "title": r.title} for r in completed],
        "active_projects": [
            {"id": r.id, "title": r.title, "deadline": r.deadline.isoformat() if r.deadline else None}
            for r in active
        ],
        "stale_projects": [{"id": r.id, "title": r.title} for r in stale],
        "new_notes_count": int(new_count or 0),
    }


async def _build_digest_sqlite(now: datetime | None) -> dict:
    from app.database import get_connection
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
    if _using_pg():
        async with async_session_factory() as session:
            timestamp = now or datetime.utcnow()
            session.add(PgNotification(
                type="digest", status="sent" if success else "failed",
                scheduled_at=timestamp, sent_at=timestamp if success else None,
                payload=json.dumps(data, default=str, ensure_ascii=False),
            ))
            await session.commit()
    else:
        from app.database import get_connection
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


async def send_weekly_review(now: datetime | None = None) -> bool:
    timestamp = now or datetime.utcnow()
    review = await generate_weekly_review(now)
    success = await send_review(review)
    if _using_pg():
        async with async_session_factory() as session:
            session.add(PgNotification(
                type="review", status="sent" if success else "failed",
                scheduled_at=timestamp, sent_at=timestamp if success else None,
                payload=json.dumps({"length": len(review)}, ensure_ascii=False),
            ))
            await session.commit()
            if success:
                try:
                    await emit_event(session, "review.generated", None, {"length": len(review)})
                except Exception:
                    logger.exception("Failed to emit review.generated")
    else:
        from app.database import get_connection
        async with get_connection() as db:
            await db.execute(
                """INSERT INTO notifications (type, status, scheduled_at, sent_at, payload)
                   VALUES ('review', ?, ?, ?, ?)""",
                (
                    "sent" if success else "failed", timestamp.isoformat(),
                    timestamp.isoformat() if success else None,
                    json.dumps({"length": len(review)}, ensure_ascii=False),
                ),
            )
            await db.commit()
            if success:
                try:
                    await emit_event(db, "review.generated", None, {"length": len(review)})
                except Exception:
                    logger.exception("Failed to emit review.generated")
    return success


# ── Auto-escalate ──────────────────────────────────────────────────────────


async def auto_escalate_urgent_notes(today: date | None = None) -> int:
    if _using_pg():
        return await _auto_escalate_pg(today)
    return await _auto_escalate_sqlite(today)


async def _auto_escalate_pg(today: date | None) -> int:
    today = today or date.today()
    deadline_cutoff = today + timedelta(days=3)
    escalated = 0
    async with async_session_factory() as session:
        rows = (await session.execute(
            select(PgNote).where(and_(
                PgNote.status == "active",
                PgNote.priority.in_(("low", "medium")),
                PgNote.deadline.isnot(None),
                PgNote.deadline <= deadline_cutoff,
            )).order_by(PgNote.deadline.asc())
        )).scalars().all()
        for note in rows:
            if note.deadline is None:
                continue
            days_left = (note.deadline - today).days
            if days_left < 0:
                continue
            old_priority = note.priority
            note.priority = "high"
            session.add(PgHistory(
                note_id=note.id, action="escalated",
                old_value=old_priority, new_value="high",
                reason=f"deadline in {days_left} days",
            ))
            note_dict = {
                "id": note.id, "title": note.title,
                "deadline": note.deadline.isoformat(),
                "priority": note.priority,
            }
            try:
                success = await notify_escalation(note_dict, days_left, old_priority)
            except Exception:
                logger.exception("notify_escalation failed for note %s", note.id)
                success = False
            session.add(PgNotification(
                note_id=note.id, type="escalation",
                status="sent" if success else "failed",
                scheduled_at=datetime.utcnow(),
                sent_at=datetime.utcnow() if success else None,
                payload=json.dumps({"old_priority": old_priority, "days_left": days_left}, ensure_ascii=False),
            ))
            escalated += 1
        if escalated:
            await session.commit()
    return escalated


async def _auto_escalate_sqlite(today: date | None) -> int:
    from app.database import get_connection
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
                deadline_date = date.fromisoformat(str(note["deadline"])[:10])
                days_left = (deadline_date - today).days
                if days_left < 0:
                    continue
                old_priority = note["priority"]
                timestamp_str = datetime.now().isoformat(sep=" ")
                await db.execute(
                    """UPDATE notes SET priority='high', updated_at=? WHERE id=?""",
                    (timestamp_str, note["id"]),
                )
                await db.execute(
                    """INSERT INTO history (note_id, action, old_value, new_value, reason)
                       VALUES (?, 'escalated', ?, 'high', ?)""",
                    (note["id"], old_priority, f"deadline in {days_left} days"),
                )
                success = await notify_escalation(note, days_left, old_priority)
                payload = json.dumps({"old_priority": old_priority, "days_left": days_left}, ensure_ascii=False)
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


# ── Embed backfill ────────────────────────────────────────────────────────


async def backfill_embeddings(batch_size: int = 20) -> int:
    """Embed notes that have embedding_status='pending' and store vectors."""
    if _using_pg():
        return await _backfill_pg(batch_size)
    return await _backfill_sqlite(batch_size)


async def _backfill_pg(batch_size: int) -> int:
    from sqlalchemy import func as sa_func
    done = 0
    async with async_session_factory() as session:
        rows = (await session.execute(
            select(PgNote)
            .where(and_(PgNote.embedding_status == "pending", PgNote.status != "archived"))
            .order_by(PgNote.created_at.asc())
            .limit(batch_size)
        )).scalars().all()
        for note in rows:
            try:
                text_to_embed = f"{note.title}\n{note.content or ''}"
                embedding = await embed_text(text_to_embed)
                if embedding is None:
                    note.embedding_status = "failed"
                else:
                    note.embedding = embedding
                    note.embedding_status = "done"
                    done += 1
            except Exception:
                logger.exception("Failed to embed note %s", note.id)
                note.embedding_status = "failed"
        await session.commit()
    if done:
        logger.info("Backfilled %d embeddings", done)
    return done


async def _backfill_sqlite(batch_size: int) -> int:
    from app.database import get_connection
    async with get_connection() as db:
        rows = await (await db.execute(
            """SELECT id, title, content FROM notes
               WHERE embedding_status = 'pending' AND status != 'archived'
               ORDER BY created_at ASC LIMIT ?""",
            (batch_size,),
        )).fetchall()
        done = 0
        for row in rows:
            try:
                text_to_embed = f"{row['title']} {row['content']}"
                await index_note(db, row["id"], text_to_embed)
                await db.execute(
                    "UPDATE notes SET embedding_status = 'done' WHERE id = ?",
                    (row["id"],),
                )
                done += 1
            except Exception:
                logger.exception("Failed to embed note %s", row["id"])
                await db.execute(
                    "UPDATE notes SET embedding_status = 'failed' WHERE id = ?",
                    (row["id"],),
                )
        await db.commit()
    if done:
        logger.info("Backfilled %d embeddings", done)
    return done


# ── Scheduler wiring ───────────────────────────────────────────────────────


def _log_job_error(event) -> None:
    logger.error("Scheduled job %r failed: %s", event.job_id, event.exception)


def _parse_digest_time() -> tuple[int, int]:
    try:
        hour_str, minute_str = settings.NOTIFY_DIGEST_TIME.split(":", 1)
        return int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        logger.warning("Invalid NOTIFY_DIGEST_TIME=%r, defaulting to 08:00", settings.NOTIFY_DIGEST_TIME)
        return 8, 0


def reclassify_trigger() -> CronTrigger:
    hours = settings.RECLASSIFY_INTERVAL_HOURS
    if not isinstance(hours, int) or hours < 1:
        logger.warning("Invalid RECLASSIFY_INTERVAL_HOURS=%r, defaulting to 6", hours)
        hours = 6
    return CronTrigger(hour=f"*/{hours}")


def digest_trigger() -> CronTrigger:
    hour, minute = _parse_digest_time()
    try:
        return CronTrigger(day_of_week=settings.NOTIFY_DIGEST_DAY, hour=hour, minute=minute)
    except (ValueError, TypeError):
        logger.warning("Invalid NOTIFY_DIGEST_DAY=%r, defaulting to 'mon'", settings.NOTIFY_DIGEST_DAY)
        return CronTrigger(day_of_week="mon", hour=hour, minute=minute)


scheduler = AsyncIOScheduler()
scheduler.add_listener(_log_job_error, EVENT_JOB_ERROR)
scheduler.add_job(
    reclassify_low_confidence_notes, reclassify_trigger(),
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
scheduler.add_job(
    send_weekly_review, CronTrigger(day_of_week="mon", hour=8, minute=0),
    id="weekly_review", name="Send weekly AI review", replace_existing=True,
)
scheduler.add_job(
    backfill_embeddings, CronTrigger(minute="*/15"),
    id="embed_backfill", name="Backfill pending embeddings", replace_existing=True,
)
scheduler.add_job(
    generate_autonomous_tasks, CronTrigger(hour=settings.AUTONOMY_DAILY_HOUR, minute=0),
    id="autonomous_tasks", name="Generate autonomous task proposals", replace_existing=True,
)
