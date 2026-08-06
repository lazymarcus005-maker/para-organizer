"""pgvector-backed embedding store for note embeddings (semantic half of hybrid RAG).

Embeddings live directly on `notes.embedding` (see app.models_v2.Note). All
functions are best-effort: if the embedding provider is unreachable, they
no-op / return an empty result rather than raising.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database_v2 import async_session_factory
from app.embed import embed_text
from app.models_v2 import Note

logger = logging.getLogger("para.vector_store")


async def index_note(session: AsyncSession | None, note_id: int, content: str) -> None:
    """Embed `content` and store its vector on `note_id`. No-op if embedding fails
    (e.g. embedding provider unreachable)."""
    embedding = await embed_text(content)
    if embedding is None:
        return
    try:
        if session is not None:
            note = await session.get(Note, note_id)
            if note is not None:
                note.embedding = embedding
                await session.flush()
        else:
            async with async_session_factory() as owned:
                note = await owned.get(Note, note_id)
                if note is not None:
                    note.embedding = embedding
                    await owned.commit()
    except Exception:
        logger.warning("Failed to store embedding for note %s", note_id, exc_info=True)


async def delete_note_embedding(session: AsyncSession | None, note_id: int) -> None:
    try:
        if session is not None:
            note = await session.get(Note, note_id)
            if note is not None:
                note.embedding = None
                await session.flush()
        else:
            async with async_session_factory() as owned:
                note = await owned.get(Note, note_id)
                if note is not None:
                    note.embedding = None
                    await owned.commit()
    except Exception:
        logger.warning("Failed to delete embedding for note %s", note_id, exc_info=True)


async def semantic_search(
    session: AsyncSession, query_embedding: list[float], limit: int = 5
) -> list[tuple[int, float]]:
    """Nearest-neighbor search by L2 distance. Returns [(note_id, score), ...]
    with score in (0.0, 1.0], highest first. Empty list if no notes have
    embeddings yet."""
    try:
        distance = Note.embedding.l2_distance(query_embedding)
        rows = (await session.execute(
            select(Note.id, distance.label("distance"))
            .where(Note.embedding.isnot(None))
            .order_by(distance)
            .limit(limit)
        )).all()
    except Exception:
        logger.warning("Semantic search failed", exc_info=True)
        return []
    return [(row.id, 1.0 / (1.0 + row.distance)) for row in rows]
