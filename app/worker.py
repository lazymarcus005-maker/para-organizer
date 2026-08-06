"""Background worker process for PARA Organizer.

Consumes tasks from the Redis task queue and executes the appropriate handler
for each topic.  Runs as a standalone process::

    python3 -m app.worker
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import date, datetime, timedelta
from functools import wraps
from typing import Any

from sqlalchemy import select, update, text as sa_text

from app.cache import get_cache
from app.config import settings
from app.database_v2 import async_session_factory
from app.models_v2 import Note, Link, History, Notification
from app.task_queue import TaskQueue, TOPICS

logger = logging.getLogger("para.worker")

# ── Retry decorator ──────────────────────────────────────────────────────────


def retry(max_attempts: int = 3, delay: float = 2.0):
    """Retry an async handler with exponential backoff."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    logger.warning(
                        "Handler %s failed (attempt %d/%d): %s",
                        func.__name__, attempt + 1, max_attempts, e,
                    )
                    await asyncio.sleep(delay * (2 ** attempt))
            return None
        return wrapper
    return decorator


# ── Topic handlers ──────────────────────────────────────────────────────────


@retry()
async def handle_classify(payload: dict[str, Any]) -> None:
    """Classify a note: call LLM, update para_category/priority/deadline/tags."""
    note_id = payload.get("note_id")
    source = payload.get("source", "manual")
    logger.info("Classify note %s (source=%s)", note_id, source)

    from app.classifier import classify_note

    async with async_session_factory() as session:
        result = await session.execute(select(Note).where(Note.id == note_id))
        note = result.scalar_one_or_none()
        if note is None:
            logger.warning("Note %s not found for classification", note_id)
            return

        classification = await classify_note(note.title, note.content)

        old_category = note.para_category
        note.para_category = classification.get("para_category", note.para_category)
        note.sub_category = classification.get("sub_category", note.sub_category)
        note.priority = classification.get("priority", note.priority)
        deadline_raw = classification.get("deadline")
        if deadline_raw:
            try:
                note.deadline = date.fromisoformat(str(deadline_raw)[:10])
            except (ValueError, TypeError):
                pass
        note.tags = classification.get("tags", note.tags or [])
        note.llm_model = classification.get("llm_model", note.llm_model)
        note.llm_confidence = float(classification.get("confidence", 0.0))
        note.llm_reasoning = classification.get("reasoning", note.llm_reasoning)

        # Log history
        session.add(History(
            note_id=note.id,
            action="classified",
            old_value=old_category,
            new_value=note.para_category,
            reason=note.llm_reasoning,
        ))
        await session.commit()
        logger.info("Note %s classified: %s (confidence=%.2f)", note_id, note.para_category, note.llm_confidence)


@retry()
async def handle_embed(payload: dict[str, Any]) -> None:
    """Generate embedding for a note and store in pgvector."""
    note_id = payload.get("note_id")
    logger.info("Embed note %s", note_id)

    from app.embed import embed_text

    async with async_session_factory() as session:
        result = await session.execute(select(Note).where(Note.id == note_id))
        note = result.scalar_one_or_none()
        if note is None:
            logger.warning("Note %s not found for embedding", note_id)
            return

        text_to_embed = f"{note.title}\n{note.content}"
        embedding = await embed_text(text_to_embed)
        if embedding is None:
            logger.warning("Failed to generate embedding for note %s", note_id)
            note.embedding_status = "failed"
            await session.commit()
            return

        note.embedding = embedding
        note.embedding_status = "done"
        await session.commit()
        logger.info("Note %s embedded (%d dimensions)", note_id, len(embedding))


@retry()
async def handle_notify(payload: dict[str, Any]) -> None:
    """Send a notification (Telegram, etc.)."""
    note_id = payload.get("note_id")
    channel = payload.get("channel", "telegram")
    logger.info("Notify note %s via %s", note_id, channel)

    from app.notifier import send_telegram

    async with async_session_factory() as session:
        result = await session.execute(select(Note).where(Note.id == note_id))
        note = result.scalar_one_or_none()
        if note is None:
            logger.warning("Note %s not found for notification", note_id)
            return

        # Build notification text
        text = (
            f"📋 {note.title}\n"
            f"📂 {note.para_category}\n"
            f"🔴 Priority: {note.priority}\n"
        )
        if note.deadline:
            text += f"📅 Deadline: {note.deadline}\n"
        text += f"\n🔗 {settings.WEB_PUBLIC_URL}/notes/{note.id}"

        # Determine chat IDs from source_metadata or defaults
        chat_ids = []
        if note.source_metadata and isinstance(note.source_metadata, dict):
            chat_id = note.source_metadata.get("chat_id")
            if chat_id:
                chat_ids.append(int(chat_id))
        if not chat_ids:
            # Fallback to configured allowed users
            chat_ids = [
                int(uid.strip())
                for uid in settings.TELEGRAM_ALLOWED_USERS.split(",")
                if uid.strip().isdigit()
            ]

        success = True
        for chat_id in chat_ids:
            ok = await send_telegram(chat_id, text)
            if not ok:
                success = False

        # Record notification
        session.add(Notification(
            note_id=note.id,
            type=payload.get("type", "manual"),
            channel=channel,
            status="sent" if success else "failed",
            scheduled_at=datetime.now(),
            sent_at=datetime.now() if success else None,
            payload={"chat_ids": chat_ids},
        ))
        await session.commit()
        logger.info("Notification for note %s: %s", note_id, "sent" if success else "failed")


@retry()
async def handle_link(payload: dict[str, Any]) -> None:
    """Auto-link a note to similar notes via semantic search."""
    note_id = payload.get("note_id")
    logger.info("Link note %s", note_id)

    from app.embed import embed_text

    async with async_session_factory() as session:
        result = await session.execute(select(Note).where(Note.id == note_id))
        note = result.scalar_one_or_none()
        if note is None:
            logger.warning("Note %s not found for linking", note_id)
            return

        # Generate embedding for the note
        text_to_embed = f"{note.title}\n{note.content}"
        embedding = await embed_text(text_to_embed)
        if embedding is None:
            logger.warning("Cannot link note %s: embedding failed", note_id)
            return

        # Search for similar notes via pgvector cosine distance
        embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
        rows = await session.execute(
            sa_text(
                """SELECT id, 1 - (embedding <=> CAST(:embedding AS vector(768))) AS similarity
                   FROM notes
                   WHERE id != :note_id AND embedding IS NOT NULL
                   ORDER BY embedding <=> CAST(:embedding2 AS vector(768))
                   LIMIT :limit"""
            ).bindparams(
                embedding=embedding_str,
                note_id=note_id,
                embedding2=embedding_str,
                limit=4,
            )
        )
        matches = [(row[0], float(row[1])) for row in rows.fetchall()]

        created = 0
        for match_id, similarity in matches:
            if similarity < 0.7:
                continue
            # Check if link already exists
            existing = await session.execute(
                select(Link).where(
                    ((Link.from_note_id == note_id) & (Link.to_note_id == match_id)) |
                    ((Link.from_note_id == match_id) & (Link.to_note_id == note_id))
                ).limit(1)
            )
            if existing.scalar_one_or_none():
                continue

            session.add(Link(
                from_note_id=note_id,
                to_note_id=match_id,
                link_type="related",
            ))
            created += 1

        if created:
            session.add(History(
                note_id=note_id,
                action="auto_linked",
                new_value=str(created),
                reason=f"auto-linked to {created} notes",
            ))
            await session.commit()
            logger.info("Note %s linked to %d similar notes", note_id, created)
        else:
            await session.commit()
            logger.info("Note %s: no new links created", note_id)


@retry()
async def handle_distill(payload: dict[str, Any]) -> None:
    """Generate a distilled summary of a note on archive."""
    note_id = payload.get("note_id")
    logger.info("Distill note %s", note_id)

    from app.distill import distill_note

    async with async_session_factory() as session:
        result = await session.execute(select(Note).where(Note.id == note_id))
        note = result.scalar_one_or_none()
        if note is None:
            logger.warning("Note %s not found for distillation", note_id)
            return

        # distill_note expects aiosqlite connection — we call the LLM part directly
        from app.classifier import call_ollama

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a note summarizer. Generate a concise 1-line summary "
                    "of the following note in the same language as the note content. "
                    "Keep it under 100 characters."
                ),
            },
            {
                "role": "user",
                "content": f"ชื่อ: {note.title}\n\nเนื้อหา:\n{note.content}",
            },
        ]

        try:
            summary = await call_ollama(
                settings.CHAT_MODEL,
                messages=messages,
                format=None,
                task="distill",
            )
            if summary and summary.strip():
                note.summary = summary.strip()
                await session.commit()
                logger.info("Note %s distilled: %s", note_id, note.summary[:60])
            else:
                logger.warning("Distillation returned empty for note %s", note_id)
        except Exception as e:
            logger.warning("Failed to distill note %s: %s", note_id, e)


@retry()
async def handle_review(payload: dict[str, Any]) -> None:
    """Generate a weekly AI review."""
    logger.info("Generate weekly review")

    from app.review import generate_weekly_review
    from app.notifier import send_review

    review = await generate_weekly_review()
    if not review:
        logger.warning("Weekly review generation returned empty")
        return

    success = await send_review(review)

    async with async_session_factory() as session:
        session.add(Notification(
            type="review",
            status="sent" if success else "failed",
            scheduled_at=datetime.now(),
            sent_at=datetime.now() if success else None,
            payload={"length": len(review)},
        ))
        await session.commit()

    logger.info("Weekly review generated (%d chars, delivered=%s)", len(review), success)


@retry()
async def handle_backup(payload: dict[str, Any]) -> None:
    """Upload data to cloud storage."""
    logger.info("Cloud backup")
    from app.backup import cloud_backup
    success = await cloud_backup()
    logger.info("Cloud backup completed: %s", "success" if success else "failed")


@retry()
async def handle_escalate(payload: dict[str, Any]) -> None:
    """Bump priority for near-deadline notes."""
    source = payload.get("source", "scheduler")
    logger.info("Escalate priorities (source=%s)", source)

    today = date.today()
    deadline_cutoff = (today + timedelta(days=3)).isoformat()
    escalated = 0

    async with async_session_factory() as session:
        # Find notes with deadline within 3 days and low/medium priority
        result = await session.execute(
            select(Note).where(
                Note.status == "active",
                Note.priority.in_(["low", "medium"]),
                Note.deadline.isnot(None),
                Note.deadline <= deadline_cutoff,
            ).order_by(Note.deadline.asc())
        )
        notes = result.scalars().all()

        for note in notes:
            if note.deadline is None:
                continue
            days_left = (note.deadline - today).days
            if days_left < 0:
                continue

            old_priority = note.priority
            note.priority = "high"

            session.add(History(
                note_id=note.id,
                action="escalated",
                old_value=old_priority,
                new_value="high",
                reason=f"deadline in {days_left} days",
            ))
            escalated += 1

        if escalated:
            await session.commit()
            logger.info("Escalated %d notes to high priority", escalated)
        else:
            logger.info("No notes to escalate")


# ── Handler registry ───────────────────────────────────────────────────────

HANDLERS: dict[str, Any] = {
    "classify": handle_classify,
    "embed": handle_embed,
    "notify": handle_notify,
    "link": handle_link,
    "distill": handle_distill,
    "review": handle_review,
    "backup": handle_backup,
    "escalate": handle_escalate,
}


# ── Worker loop ──────────────────────────────────────────────────────────────


async def worker_loop(queue: TaskQueue) -> None:
    """Main worker loop: poll all topics and dispatch tasks."""
    logger.info("Worker started — polling %d topics", len(TOPICS))
    while True:
        for topic in TOPICS:
            tasks = await queue.consume(topic, batch=5)
            for payload in tasks:
                handler = HANDLERS.get(topic)
                if handler is None:
                    logger.warning("No handler for topic %s", topic)
                    continue
                try:
                    await handler(payload)
                except Exception:
                    logger.exception("Handler failed for topic %s", topic)
        await asyncio.sleep(1)


# ── Graceful shutdown ──────────────────────────────────────────────────────


_shutdown_event = asyncio.Event()


def _signal_handler() -> None:
    logger.info("Received SIGTERM/SIGINT, shutting down...")
    _shutdown_event.set()


async def main() -> None:
    """Entry point for the worker process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    queue = TaskQueue()
    try:
        worker_task = asyncio.create_task(worker_loop(queue))
        await _shutdown_event.wait()
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    finally:
        await queue.close()
        logger.info("Worker shut down gracefully")


if __name__ == "__main__":
    asyncio.run(main())
