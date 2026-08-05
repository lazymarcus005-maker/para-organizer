"""Standalone scheduler service for PARA Organizer.

Runs APScheduler in a separate process, pushing jobs to the Redis task queue
instead of executing them directly.  Uses a Redis singleton lock to ensure
only one scheduler instance runs at a time::

    python3 -m app.scheduler_service
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.task_queue import TaskQueue

logger = logging.getLogger("para.scheduler_service")

# ── Redis singleton lock ────────────────────────────────────────────────────

_LOCK_KEY = "para:scheduler:lock"
_LOCK_TTL = 30  # seconds — renew every 30s
_LOCK_RENEW_INTERVAL = 20  # seconds between renew attempts


class SchedulerLock:
    """Redis-based singleton lock for the scheduler service.

    Uses ``SET NX`` to acquire and a background task to renew the TTL.
    """

    def __init__(self, queue: TaskQueue) -> None:
        self._queue = queue
        self._renew_task: asyncio.Task[None] | None = None
        self._held = False

    async def acquire(self) -> bool:
        r = await self._queue._get_redis()
        acquired = await r.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL)
        if acquired:
            self._held = True
            self._renew_task = asyncio.create_task(self._renew_loop())
            logger.info("Scheduler lock acquired")
        else:
            logger.info("Scheduler lock held by another instance")
        return bool(acquired)

    async def _renew_loop(self) -> None:
        while self._held:
            await asyncio.sleep(_LOCK_RENEW_INTERVAL)
            try:
                r = await self._queue._get_redis()
                await r.expire(_LOCK_KEY, _LOCK_TTL)
            except Exception:
                logger.warning("Failed to renew scheduler lock")

    async def release(self) -> None:
        self._held = False
        if self._renew_task:
            self._renew_task.cancel()
            try:
                await self._renew_task
            except asyncio.CancelledError:
                pass
        try:
            r = await self._queue._get_redis()
            await r.delete(_LOCK_KEY)
        except Exception:
            logger.warning("Failed to release scheduler lock")
        logger.info("Scheduler lock released")


# ── Job definitions ────────────────────────────────────────────────────────


def _push_job(topic: str, payload: dict[str, Any] | None = None) -> None:
    """Push a job to the Redis task queue (called by APScheduler)."""
    # APScheduler runs synchronously in the event loop thread, so we schedule
    # the async call via asyncio.create_task.
    async def _do_push() -> None:
        async with TaskQueue() as q:
            await q.publish(topic, payload or {})

    asyncio.create_task(_do_push())


def _make_jobs(scheduler: AsyncIOScheduler) -> None:
    """Register all scheduled jobs."""

    scheduler.add_job(
        _push_job,
        IntervalTrigger(hours=6),
        args=["classify", {"source": "scheduler"}],
        id="reclassify",
        name="Reclassify all notes",
        replace_existing=True,
    )

    scheduler.add_job(
        _push_job,
        CronTrigger(hour=2, minute=0),
        args=["classify", {"source": "auto_archive"}],
        id="auto_archive",
        name="Auto-archive stale notes",
        replace_existing=True,
    )

    scheduler.add_job(
        _push_job,
        CronTrigger(hour=7, minute=0),
        args=["escalate", {}],
        id="escalate",
        name="Escalate near-deadline priorities",
        replace_existing=True,
    )

    scheduler.add_job(
        _push_job,
        CronTrigger(hour=9, minute=0),
        args=["notify", {"type": "deadline"}],
        id="deadline_check",
        name="Deadline reminders",
        replace_existing=True,
    )

    scheduler.add_job(
        _push_job,
        CronTrigger(hour=18, minute=0),
        args=["notify", {"type": "stale"}],
        id="stale_check",
        name="Stale note reminders",
        replace_existing=True,
    )

    scheduler.add_job(
        _push_job,
        CronTrigger(day_of_week="mon", hour=8, minute=0),
        args=["notify", {"type": "digest"}],
        id="weekly_digest",
        name="Weekly digest",
        replace_existing=True,
    )

    scheduler.add_job(
        _push_job,
        CronTrigger(day_of_week="mon", hour=8, minute=0),
        args=["review", {}],
        id="weekly_review",
        name="Weekly AI review",
        replace_existing=True,
    )

    scheduler.add_job(
        _push_job,
        IntervalTrigger(minutes=15),
        args=["embed", {"source": "backfill"}],
        id="embed_backfill",
        name="Embedding backfill",
        replace_existing=True,
    )

    scheduler.add_job(
        _push_job,
        CronTrigger(hour=6, minute=0),
        args=["escalate", {"source": "autonomy"}],
        id="autonomous_tasks",
        name="Autonomous task generation",
        replace_existing=True,
    )

    scheduler.add_job(
        _push_job,
        CronTrigger(hour=3, minute=0),
        args=["backup", {}],
        id="cloud_backup",
        name="Cloud backup",
        replace_existing=True,
    )

    logger.info("Registered %d scheduled jobs", len(scheduler.get_jobs()))


# ── Main ────────────────────────────────────────────────────────────────────


_shutdown_event = asyncio.Event()


def _signal_handler() -> None:
    logger.info("Received SIGTERM/SIGINT, shutting down...")
    _shutdown_event.set()


async def main() -> None:
    """Entry point for the scheduler service."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    queue = TaskQueue()
    lock = SchedulerLock(queue)

    if not await lock.acquire():
        logger.info("Another scheduler instance is active — exiting")
        await queue.close()
        return

    scheduler = AsyncIOScheduler()
    _make_jobs(scheduler)
    scheduler.start()
    logger.info("Scheduler service started")

    try:
        await _shutdown_event.wait()
    finally:
        scheduler.shutdown(wait=False)
        await lock.release()
        await queue.close()
        logger.info("Scheduler service shut down gracefully")


if __name__ == "__main__":
    asyncio.run(main())
