"""/api/search — FTS5 full-text search, plus /api/context injection (SB-03)."""

import html

import aiosqlite
from fastapi import APIRouter, Depends, Query

from app.context import build_context
from app.database import get_db
from app.utils import row_to_note

router = APIRouter(prefix="/api/search", tags=["search"])
context_router = APIRouter(prefix="/api", tags=["context"])

# Delimiters passed to SQLite's snippet() that are then HTML-escaped along with
# the rest of the snippet before being swapped for real <mark> tags — this keeps
# note content that happens to contain "<"/">"/"&" from being rendered as raw
# HTML (stored XSS) when the snippet is shown with the `|safe` filter.
_SNIPPET_START = "\x01"
_SNIPPET_END = "\x02"


def _build_match_query(q: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression (prefix OR per token)."""
    tokens = [t.replace('"', '') for t in q.split() if t.strip()]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"*' for t in tokens)


def _escape_snippet(raw: str) -> str:
    """HTML-escape a snippet's text while preserving our <mark> highlight tags."""
    return (
        html.escape(raw)
        .replace(_SNIPPET_START, "<mark>")
        .replace(_SNIPPET_END, "</mark>")
    )


@router.get("")
async def search_notes(
    q: str = Query(default=""),
    limit: int = Query(default=20, le=100),
    db: aiosqlite.Connection = Depends(get_db),
):
    match_query = _build_match_query(q)
    if not match_query:
        return {"results": [], "total": 0}

    cursor = await db.execute(
        f"""
        SELECT notes.*, bm25(notes_fts) AS rank,
               snippet(notes_fts, 1, '{_SNIPPET_START}', '{_SNIPPET_END}', '...', 10) AS snippet
        FROM notes_fts
        JOIN notes ON notes.id = notes_fts.rowid
        WHERE notes_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (match_query, limit),
    )
    rows = await cursor.fetchall()

    results = []
    for row in rows:
        note = row_to_note(row)
        results.append({
            "id": note["id"],
            "title": note["title"],
            "snippet": _escape_snippet(row["snippet"]),
            "para_category": note["para_category"],
            "priority": note["priority"],
            "tags": note["tags"],
            "rank": row["rank"],
        })

    return {"results": results, "total": len(results)}


@router.get("/suggest")
async def search_suggest(
    q: str = Query(default=""),
    limit: int = Query(default=5, le=20),
    db: aiosqlite.Connection = Depends(get_db),
):
    match_query = _build_match_query(q)
    if not match_query:
        return {"suggestions": []}

    cursor = await db.execute(
        """
        SELECT notes.id, notes.title, notes.para_category
        FROM notes_fts
        JOIN notes ON notes.id = notes_fts.rowid
        WHERE notes_fts MATCH ?
        ORDER BY bm25(notes_fts)
        LIMIT ?
        """,
        (match_query, limit),
    )
    rows = await cursor.fetchall()

    return {"suggestions": [dict(r) for r in rows]}


@context_router.get("/context")
async def get_context(
    topic: str = Query(default=""),
    limit: int = Query(default=5, le=20),
):
    return await build_context(topic, limit)
