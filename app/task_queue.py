"""Redis-backed task queue for PARA Organizer.

Provides a lightweight pub/sub task queue with support for delayed tasks
(via Redis sorted sets). Used by the scheduler service to enqueue work and
by the background worker to consume it.

Topics
------
- ``classify`` — LLM classification of a note
- ``embed`` — generate and store embedding
- ``notify`` — send notification (Telegram, etc.)
- ``link`` — auto-link note to similar notes
- ``distill`` — generate note summary on archive
- ``review`` — generate weekly AI review
- ``backup`` — upload to cloud storage
- ``escalate`` — bump priority for near-deadline notes
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger("para.task_queue")

# All valid topics
TOPICS = frozenset({
    "classify",
    "embed",
    "notify",
    "link",
    "distill",
    "review",
    "backup",
    "escalate",
})

# Redis key prefixes
_QUEUE_PREFIX = "queue:"
_DELAY_PREFIX = "delay:"


class TaskQueue:
    """Async Redis-backed task queue.

    Usage::

        queue = TaskQueue()
        await queue.publish("classify", {"note_id": 42})
        tasks = await queue.consume("classify")
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self._redis_url = redis_url or settings.PARA_REDIS_URL
        self._redis: aioredis.Redis | None = None

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
            )
        return self._redis

    async def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        delay: int = 0,
    ) -> None:
        """Publish a task to the given topic.

        Args:
            topic: One of the valid topic names.
            payload: JSON-serialisable dict with task data.
            delay: Delay in seconds (0 = immediate).
        """
        if topic not in TOPICS:
            raise ValueError(f"Unknown topic: {topic!r}. Valid: {sorted(TOPICS)}")

        r = await self._get_redis()
        data = json.dumps(payload, ensure_ascii=False, default=str)

        if delay > 0:
            # Store in a sorted set with the scheduled timestamp as score
            delay_key = f"{_DELAY_PREFIX}{topic}"
            scheduled_ts = time.time() + delay
            await r.zadd(delay_key, {data: scheduled_ts})
            logger.debug("Delayed task queued: %s (in %ds)", topic, delay)
        else:
            queue_key = f"{_QUEUE_PREFIX}{topic}"
            await r.lpush(queue_key, data)
            logger.debug("Task published: %s", topic)

    async def consume(
        self,
        topic: str,
        batch: int = 1,
    ) -> list[dict[str, Any]]:
        """Consume tasks from the given topic.

        Also moves any delayed tasks whose scheduled time has passed into
        the live queue before consuming.

        Args:
            topic: One of the valid topic names.
            batch: Max number of tasks to return (default 1).

        Returns:
            List of task payload dicts.
        """
        if topic not in TOPICS:
            raise ValueError(f"Unknown topic: {topic!r}. Valid: {sorted(TOPICS)}")

        r = await self._get_redis()

        # Move expired delayed tasks into the live queue
        await self._flush_delayed(r, topic)

        queue_key = f"{_QUEUE_PREFIX}{topic}"
        tasks: list[dict[str, Any]] = []
        for _ in range(batch):
            raw = await r.rpop(queue_key)
            if raw is None:
                break
            try:
                tasks.append(json.loads(raw))
            except json.JSONDecodeError:
                logger.warning("Ignoring malformed task payload: %s", raw[:200])

        return tasks

    async def _flush_delayed(self, r: aioredis.Redis, topic: str) -> None:
        """Move expired delayed tasks into the live queue."""
        delay_key = f"{_DELAY_PREFIX}{topic}"
        now = time.time()

        # Fetch all entries with score <= now
        expired = await r.zrangebyscore(delay_key, "-inf", now)
        if not expired:
            return

        queue_key = f"{_QUEUE_PREFIX}{topic}"
        for item in expired:
            await r.lpush(queue_key, item)
        await r.zremrangebyscore(delay_key, "-inf", now)

        if expired:
            logger.debug("Moved %d delayed tasks to %s", len(expired), topic)

    async def queue_depth(self, topic: str | None = None) -> dict[str, int]:
        """Return approximate queue depth per topic (or for a single topic)."""
        r = await self._get_redis()
        topics = [topic] if topic else list(TOPICS)
        depths: dict[str, int] = {}
        for t in topics:
            queue_key = f"{_QUEUE_PREFIX}{t}"
            depth = await r.llen(queue_key)
            if depth > 0:
                depths[t] = depth
        return depths

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def __aenter__(self) -> "TaskQueue":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
