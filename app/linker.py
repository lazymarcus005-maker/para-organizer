"""Semantic auto-linking — after a note is created, find the most similar
existing notes (via pgvector) and create `related` links.

Similarity is the normalized score returned by app.vector_store.semantic_search
(derived from L2 distance, in the range (0.0, 1.0], higher = more similar), so a
`similarity_threshold` of 0.7 keeps only reasonably close neighbors. All functions
are best-effort: if embeddings are unavailable (provider down, no notes embedded
yet), they return an empty result / create no links rather than raising.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database_v2 import async_session_factory
from app.embed import embed_text
from app.models_v2 import History, Link, Note
from app.vector_store import semantic_search

logger = logging.getLogger("para.linker")


@asynccontextmanager
async def _session(session: AsyncSession | None):
    """Yield the caller-supplied session, or open a short-lived one."""
    if session is not None:
        yield session
    else:
        async with async_session_factory() as owned:
            yield owned


async def suggest_links(
    note_id: int,
    similarity_threshold: float = 0.7,
    top_k: int = 3,
    db: AsyncSession | None = None,
) -> list[dict]:
    """Find semantically similar notes via embedding similarity.

    Returns up to `top_k` matches above `similarity_threshold`, excluding the
    note itself, ordered most-similar first:
        [{"note_id": X, "similarity": 0.85, "title": "..."}, ...]

    Returns [] if the note doesn't exist, has no embeddable content, or the
    embedding/vector store is unavailable.
    """
    async with _session(db) as session:
        note = await session.get(Note, note_id)
        if note is None:
            return []

        embedding = await embed_text(note.content)
        if embedding is None:
            return []

        # Fetch one extra to leave room for excluding the note itself, which
        # will normally be the closest match to its own embedding.
        matches = await semantic_search(session, embedding, limit=top_k + 1)

        results: list[dict] = []
        for match_id, similarity in matches:
            if match_id == note_id:
                continue
            if similarity < similarity_threshold:
                continue
            title = (await session.execute(
                select(Note.title).where(Note.id == match_id)
            )).scalar_one_or_none()
            results.append({
                "note_id": match_id,
                "similarity": similarity,
                "title": title,
            })
            if len(results) >= top_k:
                break
        return results


async def auto_link_note(
    session: AsyncSession | None,
    note_id: int,
    similarity_threshold: float = 0.7,
    top_k: int = 3,
) -> int:
    """Create `related` links from `note_id` to its top semantic matches.

    Skips pairs that are already linked in either direction, logs a single
    'auto_linked' history entry when at least one link is created, and returns
    the number of links created. Never raises — embedding failures yield 0.
    """
    try:
        suggestions = await suggest_links(note_id, similarity_threshold, top_k, db=session)
    except Exception:
        logger.warning("suggest_links failed for note %s", note_id, exc_info=True)
        return 0

    async with _session(session) as db:
        created = 0
        for suggestion in suggestions:
            other_id = suggestion["note_id"]
            existing = (await db.execute(
                select(Link.id).where(
                    ((Link.from_note_id == note_id) & (Link.to_note_id == other_id))
                    | ((Link.from_note_id == other_id) & (Link.to_note_id == note_id))
                ).limit(1)
            )).scalar_one_or_none()
            if existing is not None:
                continue
            db.add(Link(from_note_id=note_id, to_note_id=other_id, link_type="related"))
            created += 1

        if created:
            db.add(History(
                note_id=note_id, action="auto_linked",
                new_value=str(created), reason=f"auto-linked to {created} notes",
            ))
        if session is None:
            await db.commit()
        else:
            await db.flush()
        return created
