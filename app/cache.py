"""Redis cache layer for PARA Organizer.

Provides a simple key-value cache backed by Redis with standard TTL support.
Cache keys follow a consistent naming pattern for easy invalidation.

Cache key patterns
------------------
- ``para:stats:{hash}`` — stats response (TTL 60s)
- ``para:digest:{date}`` — digest response (TTL 300s)
- ``para:deadlines:{days}`` — deadlines response (TTL 120s)
- ``para:tree`` — PARA tree (TTL 60s)
- ``para:note:{id}`` — single note (TTL 300s)
- ``para:graph:full`` — full graph (TTL 300s)
- ``para:settings`` — settings (TTL 60s)
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger("para.cache")

# Default TTLs per key pattern (seconds)
DEFAULT_TTL: int = settings.PARA_REDIS_CACHE_TTL
TTL_OVERRIDES: dict[str, int] = {
    "para:stats:": 60,
    "para:digest:": 300,
    "para:deadlines:": 120,
    "para:tree": 60,
    "para:note:": 300,
    "para:graph:full": 300,
    "para:settings": 60,
}


def _get_ttl(key: str) -> int:
    """Return the TTL for a given cache key based on pattern matching."""
    for pattern, ttl in TTL_OVERRIDES.items():
        if key.startswith(pattern):
            return ttl
    return DEFAULT_TTL


def stats_key(**filters: Any) -> str:
    """Build a deterministic cache key for stats with the given filters.

    The filters are sorted by key and MD5-hashed to keep keys short.
    """
    if not filters:
        return "para:stats:default"
    raw = json.dumps(filters, sort_keys=True, ensure_ascii=False)
    h = hashlib.md5(raw.encode()).hexdigest()
    return f"para:stats:{h}"


class Cache:
    """Async Redis cache layer.

    Usage::

        cache = Cache()
        await cache.set("para:tree", tree_data)
        data = await cache.get("para:tree")
        await cache.invalidate("para:note:*")
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

    async def get(self, key: str) -> Any | None:
        """Get a value from the cache.

        Returns the deserialized value, or ``None`` if the key doesn't exist.
        """
        r = await self._get_redis()
        raw = await r.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Cache: malformed JSON for key %s", key)
            return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
    ) -> None:
        """Set a value in the cache.

        Args:
            key: Cache key.
            value: JSON-serialisable value.
            ttl: TTL in seconds. If ``None``, uses the pattern-matched default.
        """
        r = await self._get_redis()
        data = json.dumps(value, ensure_ascii=False, default=str)
        expire = ttl if ttl is not None else _get_ttl(key)
        await r.setex(key, expire, data)

    async def invalidate(self, pattern: str) -> int:
        """Invalidate (delete) all keys matching a glob pattern.

        Args:
            pattern: Redis glob pattern, e.g. ``para:note:*`` or ``para:stats:*``.

        Returns:
            Number of keys deleted.
        """
        r = await self._get_redis()
        cursor = 0
        deleted = 0
        while True:
            cursor, keys = await r.scan(cursor, match=pattern, count=100)
            if keys:
                deleted += await r.delete(*keys)
            if cursor == 0:
                break
        if deleted:
            logger.debug("Cache invalidated: %s (%d keys)", pattern, deleted)
        return deleted

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def __aenter__(self) -> "Cache":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
