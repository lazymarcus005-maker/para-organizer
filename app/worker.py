"""Background worker process for PARA Organizer.

Consumes tasks from the Redis task queue and dispatches them to the
appropriate handler.  Designed to run as a standalone process::

    python3 -m app.worker

Graceful shutdown is handled via SIGTERM / SIGINT.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Any

# Ensure project root is on sys.path when run as ``python3 -m app.worker``
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.task_queue import TaskQueue, TOPICS

logger = logging.getLogger("para.worker")

# ── Global shutdown flag ─────────────────────────────────────────────────────
_shutdown = asyncio.Event()


def _handle_signal(sig: int, _frame: Any) -> None:
    logger.info("Received signal %s, shutting down...", signal.Signals(sig).name)
    _shutdown.set()


# ── Topic handlers ───────────────────────────────────────────────────────────

async def handle_classify(payload: dict[str, Any]) -> None:
    """Call LLM to classify a note, updating para_category/priority/deadline/tags."""
    note_id = payload.get("note_id")
    if not note_id:
        logger.warning("classify: missing note_id in payload")
        return
    logger.info("classify: note_id=%s", note_id)
    # TODO: import and call classifier, update note in DB
    # from app.classifier import classify_note
    # from app.database_v2 import async_session_factory
    # async with async_session_factory() as session:
    #     note = await session.get(Note, note_id)
    #     result = await classify_note(note.title, note.content)
    #     ... update note fields ...
    #     await session.commit()


async def handle_embed(payload: dict[str, Any]) -> None:
    """Generate embedding for a note and store in pgvector."""
    note_id = payload.get("note_id")
    if not note_id:
        logger.warning("embed: missing note_id in payload")
        return
    logger.info("embed: note_id=%s", note_id)
    # TODO: generate embedding and store
    # from app.embed import generate_embedding
    # from app.database_v2 import async_session_factory
    # async with async_session_factory() as session:
    #     note = await session.get(Note, note_id)
    #     emb = await generate_embedding(note.content)
    #     note.embedding = emb
    #     note.embedding_status = "done"
    #     await session.commit()


async def handle_notify(payload: dict[str, Any]) -> None:
    """Send a notification (Telegram message)."""
    logger.info("notify: %s", payload)
    # TODO: send Telegram message
    # from app.notifier import send_notification
    # await send_notification(payload)


async def handle_link(payload: dict[str, Any]) -> None:
    """Auto-link a note to similar notes."""
    note_id = payload.get("note_id")
    if not note_id:
        logger.warning("link: missing note_id in payload")
        return
    logger.info("link: note_id=%s", note_id)
    # TODO: auto-link note
    # from app.linker import auto_link_note
    # from app.database_v2 import async_session_factory
    # async with async_session_factory() as session:
    #     await auto_link_note(session, note_id)


async def handle_distill(payload: dict[str, Any]) -> None:
    """Generate a summary of a note on archive."""
    note_id = payload.get("note_id")
    if not note_id:
        logger.warning("distill: missing note_id in payload")
        return
    logger.info("distill: note_id=%s", note_id)
    # TODO: generate distillation
    # from app.distill import distill_note
    # await distill_note(note_id)


async def handle_review(payload: dict[str, Any]) -> None:
    """Generate a weekly AI review."""
    logger.info("review: %s", payload)
    # TODO: generate weekly review
    # from app.review import generate_weekly_review
    # await generate_weekly_review()


async def handle_backup(payload: dict[str, Any]) -> None:
    """Upload database to cloud storage."""
    logger.info("backup: %s", payload)
    # TODO: perform cloud backup
    # from app.backup import cloud_backup
    # await cloud_backup()


async def handle_escalate(payload: dict[str, Any]) -> None:
    """Bump priority for near-deadline notes."""
    logger.info("escalate: %s", payload)
    # TODO: escalate near-deadline notes
    # from app.database_v2 import async_session_factory
    # async with async_session_factory() as session:
    #     ... update priorities ...


# ── Handler registry ─────────────────────────────────────────────────────────

_HANDLERS: dict[str, Any] = {
    "classify": handle_classify,
    "embed": handle_embed,
    "notify": handle_notify,
    "link": handle_link,
    "distill": handle_distill,
    "review": handle_review,
    "backup": handle_backup,
    "escalate": handle_escalate,
}


# ── Main loop ───────────────────────────────────────────────────────────────

async def _process_topic(queue: TaskQueue, topic: str) -> None:
    """Process one batch of tasks from a single topic."""
    tasks = await queue.consume(topic, batch=5)
    for payload in tasks:
        handler = _HANDLERS.get(topic)
        if handler is None:
            logger.warning("No handler for topic: %s", topic)
            continue
        try:
            await handler(payload)
        except Exception:
            logger.exception("Handler failed for topic %s: %s", topic, payload)


async def run_worker() -> None:
    """Main worker loop — polls all topics in a round-robin fashion."""
    logger.info("Worker starting (poll interval: 1s)")

    queue = TaskQueue()
    try:
        while not _shutdown.is_set():
            for topic in sorted(TOPICS):
                if _shutdown.is_set():
                    break
                try:
                    await _process_topic(queue, topic)
                except Exception:
                    logger.exception("Error processing topic %s", topic)
            # Brief sleep between poll cycles
            await asyncio.sleep(1)
    finally:
        await queue.close()
        logger.info("Worker shut down gracefully")


def main() -> None:
    """Entry point for ``python3 -m app.worker``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Register signal handlers
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: _handle_signal(s, None))

    try:
        loop.run_until_complete(run_worker())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
