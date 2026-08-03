"""SQLAlchemy async engine, session factory, and FastAPI dependency for PostgreSQL.

Provides the async database layer used by the distributed phase (phase 0) of the
PARA Organizer.  Replaces the aiosqlite-based connection management in database.py
for deployments that target PostgreSQL instead of SQLite.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import text

from app.config import settings

logger = logging.getLogger("para.database_v2")

# ── Engine ──────────────────────────────────────────────────────────────────

engine = create_async_engine(
    settings.PARA_DB_URL,
    pool_size=settings.PARA_DB_POOL_SIZE,
    max_overflow=settings.PARA_DB_MAX_OVERFLOW,
    echo=False,
    pool_pre_ping=True,
)

# ── Session factory ──────────────────────────────────────────────────────────

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async SQLAlchemy session.

    Usage::

        from fastapi import Depends
        from sqlalchemy.ext.asyncio import AsyncSession
        from app.database_v2 import get_db

        @router.get("/items")
        async def list_items(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_db() -> bool:
    """Health-check: returns ``True`` if the database is reachable."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("Database health check failed")
        return False
