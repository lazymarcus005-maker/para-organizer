"""sqlite-vec backed vector store for note embeddings (semantic half of hybrid RAG).

Embeddings live in the `note_embeddings` vec0 virtual table (see app.database),
keyed by note id as rowid. All functions are best-effort: if the sqlite-vec
extension failed to load (see app.database._load_vec_extension) the underlying
table doesn't exist, so queries raise aiosqlite.Error, which is caught here and
turned into a no-op / empty result rather than a crash.
"""

from __future__ import annotations

import json
import logging

import aiosqlite

from app.embed import embed_text

logger = logging.getLogger("para.vector_store")


async def index_note(db: aiosqlite.Connection, note_id: int, content: str) -> None:
    """Embed `content` and upsert its vector for `note_id`. No-op if embedding
    or storage fails (e.g. embedding provider unreachable, vec0 table absent)."""
    embedding = await embed_text(content)
    if embedding is None:
        return
    try:
        await db.execute("DELETE FROM note_embeddings WHERE rowid = ?", (note_id,))
        await db.execute(
            "INSERT INTO note_embeddings(rowid, embedding) VALUES (?, ?)",
            (note_id, json.dumps(embedding)),
        )
        await db.commit()
    except aiosqlite.Error:
        logger.warning("Failed to store embedding for note %s", note_id, exc_info=True)


async def delete_note_embedding(db: aiosqlite.Connection, note_id: int) -> None:
    try:
        await db.execute("DELETE FROM note_embeddings WHERE rowid = ?", (note_id,))
        await db.commit()
    except aiosqlite.Error:
        logger.warning("Failed to delete embedding for note %s", note_id, exc_info=True)


async def semantic_search(
    db: aiosqlite.Connection, query_embedding: list[float], limit: int = 5
) -> list[tuple[int, float]]:
    """Nearest-neighbor search by L2 distance. Returns [(note_id, score), ...]
    with score in (0.0, 1.0], highest first. Empty list if the vector table is
    unavailable or empty."""
    query_json = json.dumps(query_embedding)
    try:
        cursor = await db.execute(
            """SELECT rowid, vec_distance_L2(embedding, ?) AS distance
               FROM note_embeddings
               ORDER BY distance
               LIMIT ?""",
            (query_json, limit),
        )
        rows = await cursor.fetchall()
    except aiosqlite.Error:
        logger.warning("Semantic search failed", exc_info=True)
        return []
    return [(row["rowid"], 1.0 / (1.0 + row["distance"])) for row in rows]
