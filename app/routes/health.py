"""Health check endpoints for PARA Organizer v5.

Provides liveness, readiness, and detailed health probes for
container orchestration and monitoring.
"""

import logging
import os
import time
from datetime import datetime

from fastapi import APIRouter

logger = logging.getLogger("para.health")

_START_TIME = time.time()

router = APIRouter(prefix="/api", tags=["health"])


async def _check_db() -> str:
    """Check database connectivity.

    Returns "connected" if reachable, "disconnected" otherwise.
    """
    try:
        from app.database import get_connection

        async with get_connection() as db:
            await db.execute("SELECT 1")
        return "connected"
    except Exception as exc:
        logger.warning("Health check — DB unreachable: %s", exc)
        return "disconnected"


async def _check_redis() -> str:
    """Check Redis connectivity.

    Returns "connected" if reachable, "disconnected" otherwise.
    """
    redis_url = os.environ.get("PARA_REDIS_URL", "")
    if not redis_url:
        return "disconnected"
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(redis_url, socket_connect_timeout=2)
        await r.ping()
        await r.aclose()
        return "connected"
    except Exception as exc:
        logger.warning("Health check — Redis unreachable: %s", exc)
        return "disconnected"


async def _queue_depth() -> dict:
    """Return approximate queue depth per topic (Redis-based task queue)."""
    redis_url = os.environ.get("PARA_REDIS_URL", "")
    if not redis_url:
        return {}
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(redis_url, socket_connect_timeout=2)
        keys = await r.keys("queue:*")
        depths: dict[str, int] = {}
        for key in keys:
            topic = key.decode() if isinstance(key, bytes) else key
            depth = await r.llen(key)
            depths[topic] = depth
        await r.aclose()
        return depths
    except Exception:
        return {}


async def _cache_hit_ratio() -> float:
    """Return cache hit ratio from Redis INFO stats."""
    redis_url = os.environ.get("PARA_REDIS_URL", "")
    if not redis_url:
        return 0.0
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(redis_url, socket_connect_timeout=2)
        info = await r.info("stats")
        hits = int(info.get("keyspace_hits", 0))
        misses = int(info.get("keyspace_misses", 0))
        await r.aclose()
        total = hits + misses
        return round(hits / total, 4) if total > 0 else 0.0
    except Exception:
        return 0.0


@router.get("/health")
async def health():
    """Detailed health dashboard.

    Returns:
        JSON with status, version, db/redis connectivity, queue depth,
        cache hit ratio, and uptime.
    """
    db_status = await _check_db()
    redis_status = await _check_redis()

    if db_status == "connected" and redis_status == "connected":
        overall = "healthy"
    elif db_status == "disconnected" and redis_status == "disconnected":
        overall = "unhealthy"
    else:
        overall = "degraded"

    return {
        "status": overall,
        "version": "5.0.0",
        "db": db_status,
        "redis": redis_status,
        "queue_depth": await _queue_depth(),
        "cache_hit_ratio": await _cache_hit_ratio(),
        "uptime_seconds": int(time.time() - _START_TIME),
    }


@router.get("/health/ready")
async def ready():
    """Readiness probe — returns 200 only when DB and Redis are reachable.

    Returns:
        200 OK with {"status": "ready"} if both DB and Redis are connected.
        503 Service Unavailable otherwise.
    """
    from fastapi.responses import JSONResponse

    db_status = await _check_db()
    redis_status = await _check_redis()

    if db_status == "connected" and redis_status == "connected":
        return {"status": "ready"}

    return JSONResponse(
        status_code=503,
        content={
            "status": "not_ready",
            "db": db_status,
            "redis": redis_status,
        },
    )


@router.get("/health/live")
async def live():
    """Liveness probe — returns 200 if the process is alive.

    Returns:
        200 OK with {"status": "alive"}.
    """
    return {"status": "alive"}
