"""Semantic auto-linking — after a note is created, find the most similar
existing notes (via the sqlite-vec embedding store) and create `related` links.

Similarity is the normalized score returned by app.vector_store.semantic_search
(derived from L2 distance, in the range (0.0, 1.0], higher = more similar), so a
`similarity_threshold` of 0.7 keeps only reasonably close neighbors. All functions
are best-effort: if embeddings are unavailable (provider down, vec0 table absent),
they return an empty result / create no links rather than raising.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import aiosqlite

from app.database import get_connection
from app.embed import embed_text
from app.vector_store import semantic_search

logger = logging.getLogger("para.linker")


@asynccontextmanager
async def _connection(db: aiosqlite.Connection | None):
    """Yield the caller-supplied connection, or open a short-lived one."""
    if db is not None:
        yield db
    else:
        async with get_connection() as owned:
            yield owned


async def suggest_links(
    note_id: int,
    similarity_threshold: float = 0.7,
    top_k: int = 3,
    db: aiosqlite.Connection | None = None,
) -> list[dict]:
    """Find semantically similar notes via embedding similarity.

    Returns up to `top_k` matches above `similarity_threshold`, excluding the
    note itself, ordered most-similar first:
        [{"note_id": X, "similarity": 0.85, "title": "..."}, ...]

    Returns [] if the note doesn't exist, has no embeddable content, or the
    embedding/vector store is unavailable.
    """
    async with _connection(db) as conn:
        row = await (await conn.execute(
            "SELECT content FROM notes WHERE id = ?", (note_id,)
        )).fetchone()
        if row is None:
            return []

        embedding = await embed_text(row["content"])
        if embedding is None:
            return []

        # Fetch one extra to leave room for excluding the note itself, which
        # will normally be the closest match to its own embedding.
        matches = await semantic_search(conn, embedding, limit=top_k + 1)

        results: list[dict] = []
        for match_id, similarity in matches:
            if match_id == note_id:
                continue
            if similarity < similarity_threshold:
                continue
            title_row = await (await conn.execute(
                "SELECT title FROM notes WHERE id = ?", (match_id,)
            )).fetchone()
            results.append({
                "note_id": match_id,
                "similarity": similarity,
                "title": title_row["title"] if title_row else None,
            })
            if len(results) >= top_k:
                break
        return results


async def auto_link_note(
    db: aiosqlite.Connection,
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
        suggestions = await suggest_links(note_id, similarity_threshold, top_k, db=db)
    except Exception:
        logger.warning("suggest_links failed for note %s", note_id, exc_info=True)
        return 0

    created = 0
    for suggestion in suggestions:
        other_id = suggestion["note_id"]
        existing = await (await db.execute(
            """SELECT 1 FROM links
               WHERE (from_note_id = ? AND to_note_id = ?)
                  OR (from_note_id = ? AND to_note_id = ?)
               LIMIT 1""",
            (note_id, other_id, other_id, note_id),
        )).fetchone()
        if existing:
            continue
        await db.execute(
            "INSERT INTO links (from_note_id, to_note_id, link_type) VALUES (?, ?, 'related')",
            (note_id, other_id),
        )
        created += 1

    if created:
        await db.execute(
            "INSERT INTO history (note_id, action, new_value, reason) VALUES (?, ?, ?, ?)",
            (note_id, "auto_linked", str(created), f"auto-linked to {created} notes"),
        )
    await db.commit()
    return created
