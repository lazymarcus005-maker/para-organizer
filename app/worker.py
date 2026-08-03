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
from typing import Any

from app.cache import get_cache
from app.config import settings
from app.task_queue import TaskQueue, TOPICS

logger = logging.getLogger("para.worker")

# ── Topic handlers ──────────────────────────────────────────────────────────


async def handle_classify(payload: dict[str, Any]) -> None:
    """Classify a note: call LLM, update para_category/priority/deadline/tags."""
    note_id = payload.get("note_id")
    logger.info("Classify note %s", note_id)
    # TODO: import and call classifier.classify_note(note_id)
    await asyncio.sleep(0.1)


async def handle_embed(payload: dict[str, Any]) -> None:
    """Generate embedding for a note and store in pgvector."""
    note_id = payload.get("note_id")
    logger.info("Embed note %s", note_id)
    # TODO: import and call embedder.embed_note(note_id)
    await asyncio.sleep(0.1)


async def handle_notify(payload: dict[str, Any]) -> None:
    """Send a notification (Telegram, etc.)."""
    note_id = payload.get("note_id")
    channel = payload.get("channel", "telegram")
    logger.info("Notify note %s via %s", note_id, channel)
    # TODO: import and call notifier.send(note_id, channel)
    await asyncio.sleep(0.1)


async def handle_link(payload: dict[str, Any]) -> None:
    """Auto-link a note to similar notes."""
    note_id = payload.get("note_id")
    logger.info("Link note %s", note_id)
    # TODO: import and call linker.link_note(note_id)
    await asyncio.sleep(0.1)


async def handle_distill(payload: dict[str, Any]) -> None:
    """Generate a distilled summary of a note on archive."""
    note_id = payload.get("note_id")
    logger.info("Distill note %s", note_id)
    # TODO: import and call distiller.distill(note_id)
    await asyncio.sleep(0.1)


async def handle_review(payload: dict[str, Any]) -> None:
    """Generate a weekly AI review."""
    logger.info("Generate weekly review")
    # TODO: import and call reviewer.generate_weekly_review()
    await asyncio.sleep(0.1)


async def handle_backup(payload: dict[str, Any]) -> None:
    """Upload data to cloud storage."""
    logger.info("Cloud backup")
    # TODO: import and call backup.cloud_backup()
    await asyncio.sleep(0.1)


async def handle_escalate(payload: dict[str, Any]) -> None:
    """Bump priority for near-deadline notes."""
    logger.info("Escalate priorities")
    # TODO: import and call scheduler.escalate_priorities()
    await asyncio.sleep(0.1)


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
