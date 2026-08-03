"""Redis cache layer for PARA Organizer.

Provides a lightweight caching layer backed by Redis, used to reduce
database load on read-heavy endpoints (stats, deadlines, digest, tree, graph).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger("para.cache")

# Default TTL per key pattern (seconds)
DEFAULT_TTL: int = settings.PARA_REDIS_CACHE_TTL

# Cache key prefixes
_PREFIX = "para:"


def stats_key(**filters: Any) -> str:
    """Generate a deterministic cache key for stats queries.

    Args:
        **filters: Query filter parameters (e.g. category='Projects', days=7).

    Returns:
        A cache key like ``para:stats:a1b2c3...`` where the hash is MD5 of
        the sorted JSON-serialised filter dict.
    """
    raw = json.dumps(filters, sort_keys=True, ensure_ascii=False, default=str)
    h = hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()[:12]
    return f"{_PREFIX}stats:{h}"


class Cache:
    """Redis-backed cache with get/set/invalidate operations.

    Usage::

        cache = Cache()
        await cache.set("para:tree", tree_data, ttl=60)
        data = await cache.get("para:tree")
        await cache.invalidate("para:stats:*")
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self._redis_url = redis_url or settings.PARA_REDIS_URL
        self._redis: aioredis.Redis | None = None
        self._hits = 0
        self._misses = 0

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
            )
        return self._redis

    async def get(self, key: str) -> Any | None:
        """Get a value from cache.

        Args:
            key: Full cache key (e.g. ``para:tree``).

        Returns:
            The deserialised value, or ``None`` if not found.
        """
        r = await self._get_redis()
        raw = await r.get(key)
        if raw is None:
            self._misses += 1
            return None
        self._hits += 1
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Cache corruption for key %s, evicting", key)
            await r.delete(key)
            return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
    ) -> None:
        """Set a value in cache.

        Args:
            key: Full cache key.
            value: JSON-serialisable value.
            ttl: Time-to-live in seconds (default: ``PARA_REDIS_CACHE_TTL``).
        """
        r = await self._get_redis()
        data = json.dumps(value, ensure_ascii=False, default=str)
        ttl = ttl if ttl is not None else DEFAULT_TTL
        await r.setex(key, ttl, data)

    async def invalidate(self, pattern: str) -> int:
        """Invalidate all keys matching a glob pattern.

        Args:
            pattern: Redis glob pattern (e.g. ``para:stats:*``).

        Returns:
            Number of keys deleted.
        """
        r = await self._get_redis()
        cursor = 0
        keys: list[str] = []
        while True:
            cursor, batch = await r.scan(cursor, match=pattern, count=100)
            keys.extend(str(k) for k in batch)
            if cursor == 0:
                break
        if keys:
            await r.delete(*keys)
            logger.debug("Cache invalidated %d keys matching %s", len(keys), pattern)
        return len(keys)

    async def hit_ratio(self) -> float:
        """Return the cache hit ratio since this instance was created."""
        total = self._hits + self._misses
        if total == 0:
            return 0.0
        return self._hits / total

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def __aenter__(self) -> "Cache":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


# Module-level singleton (lazily initialised)
_cache_instance: Cache | None = None


def get_cache() -> Cache:
    """Return the module-level Cache singleton."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = Cache()
    return _cache_instance
