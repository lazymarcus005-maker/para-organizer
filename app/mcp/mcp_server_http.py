"""MCP HTTP SSE server for PARA Organizer v5.

Exposes the same 15 tools as the stdio MCP server (mcp_server.py) but
over HTTP with Server-Sent Events (SSE) transport, suitable for
containerized / distributed deployments.

Run: python3 -m app.mcp.mcp_server_http (port 8100)
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from app import classifier
from app.classifier import classify_note, extract_deadline_from_text
from app.config import settings
from app.context import build_context
from app.database import get_connection, init_db
from app.distill import distill_note
from app.events import emit_event
from app.feedback import get_feedback_stats
from app.graph import get_related, get_subgraph
from app.items import compute_progress, create_item, list_items, sync_note_progress, update_item
from app.models import PARA_CATEGORIES
from app.planner import generate_plan
from app.tasks import complete_task, create_task, list_tasks
from app.utils import row_to_note, spawn_recurring_instance
from app.vector_store import delete_note_embedding

logger = logging.getLogger("para.mcp.http")

LINK_TYPES = {"related", "depends_on", "refines"}

mcp = FastMCP("para-organizer-http")


async def _log_history(db, note_id: int, action: str, old_value: str | None = None,
                       new_value: str | None = None, reason: str | None = None) -> None:
    await db.execute(
        "INSERT INTO history (note_id, action, old_value, new_value, reason) VALUES (?, ?, ?, ?, ?)",
        (note_id, action, old_value, new_value, reason),
    )


async def _fetch_note(db, note_id: int) -> dict | None:
    cursor = await db.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
    row = await cursor.fetchone()
    return row_to_note(row) if row is not None else None


@mcp.tool()
async def para_add_note(title: str, content: str) -> dict:
    """Add a note to PARA Organizer. Auto-classifies with the LLM.

    Args:
        title: Note title (short summary)
        content: Note content (details)

    Returns:
        The created note, including LLM classification fields
        (para_category, priority, deadline, tags, llm_confidence, ...).
    """
    result = await classify_note(title, content)
    para_category = result.get("para_category", "inbox")
    sub_category = result.get("sub_category")
    priority = result.get("priority", "medium")
    deadline = result.get("deadline")
    tags = result.get("tags", [])
    llm_model = result.get("llm_model")
    llm_confidence = float(result.get("confidence", 0.0))
    llm_reasoning = result.get("reasoning")

    if not deadline:
        extracted = extract_deadline_from_text(content)
        deadline = extracted.isoformat() if extracted else None

    async with get_connection() as db:
        cursor = await db.execute(
            """
            INSERT INTO notes (title, content, para_category, sub_category, priority, deadline,
                                tags, source, llm_model, llm_confidence, llm_reasoning)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title, content, para_category, sub_category, priority, deadline,
                json.dumps(tags, ensure_ascii=False), "hermes", llm_model, llm_confidence, llm_reasoning,
            ),
        )
        note_id = cursor.lastrowid
        await _log_history(db, note_id, "created", new_value="hermes")
        await _log_history(db, note_id, "classified", new_value=para_category, reason=llm_reasoning)
        await db.commit()
        return await _fetch_note(db, note_id)


@mcp.tool()
async def para_search(query: str, category: str | None = None, limit: int = 10) -> list:
    """Full-text search over notes (SQLite FTS5).

    Args:
        query: Search query (matches title, content, tags)
        category: Optional PARA category filter
        limit: Maximum number of results (default 10)

    Returns:
        List of matching notes ranked by relevance (each includes a ``rank`` key),
        or ``{"error": ...}`` if the query is malformed.
    """
    sql = """
        SELECT n.*, bm25(notes_fts) AS rank
        FROM notes_fts
        JOIN notes n ON n.id = notes_fts.rowid
        WHERE notes_fts MATCH ?
    """
    params: list = [query]
    if category:
        sql += " AND n.para_category = ?"
        params.append(category)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    async with get_connection() as db:
        try:
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
        except Exception as e:
            logger.warning("para_search failed for query %r: %s", query, e)
            return {"error": f"Search failed: {e}"}
        return [row_to_note(r) for r in rows]


@mcp.tool()
async def para_list(category: str | None = None, status: str | None = None, limit: int = 20) -> list:
    """List notes, optionally filtered by PARA category and/or status.

    Args:
        category: Optional category filter (projects|areas|resources|archives|inbox)
        status: Optional status filter (active|completed|archived)
        limit: Maximum number of results (default 20)

    Returns:
        List of notes, newest first.
    """
    clauses = []
    params: list = []
    if category:
        clauses.append("para_category = ?")
        params.append(category)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    async with get_connection() as db:
        cursor = await db.execute(
            f"SELECT * FROM notes {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        )
        rows = await cursor.fetchall()
        return [row_to_note(r) for r in rows]


@mcp.tool()
async def para_get(id: int) -> dict:
    """Get a single note by its ID.

    Args:
        id: Note ID

    Returns:
        The note, or ``{"error": ...}`` if not found.
    """
    async with get_connection() as db:
        note = await _fetch_note(db, id)
        if note is None:
            return {"error": f"Note {id} not found"}
        return note


@mcp.tool()
async def para_move(id: int, category: str) -> dict:
    """Move a note to a different PARA category.

    Args:
        id: Note ID
        category: Target category (projects|areas|resources|archives|inbox)

    Returns:
        The updated note, or ``{"error": ...}`` on invalid category / missing note.
    """
    if category not in PARA_CATEGORIES:
        return {"error": f"Invalid category '{category}'. Must be one of: {', '.join(PARA_CATEGORIES)}"}
    async with get_connection() as db:
        existing = await _fetch_note(db, id)
        if existing is None:
            return {"error": f"Note {id} not found"}
        await db.execute(
            "UPDATE notes SET para_category = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (category, id),
        )
        await _log_history(db, id, "moved", old_value=existing["para_category"], new_value=category)
        await db.commit()
        return await _fetch_note(db, id)


@mcp.tool()
async def para_archive(id: int) -> dict:
    """Archive a note (moves it to archives and marks status archived).

    Args:
        id: Note ID

    Returns:
        The updated note, or ``{"error": ...}`` if not found.
    """
    async with get_connection() as db:
        existing = await _fetch_note(db, id)
        if existing is None:
            return {"error": f"Note {id} not found"}

        summary = await distill_note(db, id)
        await db.execute(
            """UPDATE notes SET para_category = 'archives', status = 'archived',
               summary = ?, archived_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (summary, id),
        )
        await _log_history(db, id, "archived", old_value=existing["para_category"], new_value="archives")

        summary = await distill_note(db, id)
        if summary:
            await db.execute("UPDATE notes SET summary = ? WHERE id = ?", (summary, id))
            await _log_history(db, id, "distilled", new_value=summary)

        await db.commit()

        try:
            await emit_event(db, "note.completed", id, {
                "title": existing["title"],
                "para_category": "archives",
                "status": "archived",
            })
        except Exception:
            logger.warning("Failed to emit note.completed for note %d", id, exc_info=True)

        return await _fetch_note(db, id)


@mcp.tool()
async def para_stats() -> dict:
    """Summary statistics for the whole PARA system.

    Returns:
        Dict with total_notes, by_category, by_status, by_priority,
        upcoming_deadlines and avg_confidence.
    """
    async with get_connection() as db:
        total = (await (await db.execute("SELECT COUNT(*) AS c FROM notes")).fetchone())["c"]

        rows = await (await db.execute(
            "SELECT para_category, COUNT(*) AS c FROM notes GROUP BY para_category")).fetchall()
        by_category = {r["para_category"]: r["c"] for r in rows}

        rows = await (await db.execute(
            "SELECT status, COUNT(*) AS c FROM notes GROUP BY status")).fetchall()
        by_status = {r["status"]: r["c"] for r in rows}

        rows = await (await db.execute(
            "SELECT priority, COUNT(*) AS c FROM notes GROUP BY priority")).fetchall()
        by_priority = {r["priority"]: r["c"] for r in rows}

        upcoming = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM notes "
            "WHERE deadline IS NOT NULL AND status = 'active' AND deadline >= date('now')")).fetchone())["c"]

        avg_row = await (await db.execute("SELECT AVG(llm_confidence) AS avg_conf FROM notes")).fetchone()
        avg_conf = avg_row["avg_conf"] or 0.0

        return {
            "total_notes": total,
            "by_category": by_category,
            "by_status": by_status,
            "by_priority": by_priority,
            "upcoming_deadlines": upcoming,
            "avg_confidence": round(avg_conf, 3),
        }


@mcp.tool()
async def para_deadlines(days_ahead: int = 14) -> list:
    """Upcoming deadlines within the next N days (active notes only).

    Args:
        days_ahead: Look-ahead window in days (default 14)

    Returns:
        List of ``{id, title, deadline, days_left, priority}``, soonest first.
    """
    today = date.today()
    horizon = (today + timedelta(days=days_ahead)).isoformat()
    async with get_connection() as db:
        cursor = await db.execute(
            """SELECT id, title, deadline, priority FROM notes
               WHERE deadline IS NOT NULL AND status = 'active'
                 AND deadline >= ? AND deadline <= ?
               ORDER BY deadline ASC""",
            (today.isoformat(), horizon),
        )
        rows = await cursor.fetchall()
        results = []
        for r in rows:
            results.append({
                "id": r["id"],
                "title": r["title"],
                "deadline": r["deadline"],
                "days_left": (date.fromisoformat(r["deadline"]) - today).days,
                "priority": r["priority"],
            })
        return results


@mcp.tool()
async def para_digest() -> dict:
    """Weekly digest: totals, completed this week, active and stale projects, new notes.

    Returns:
        Digest dict with total_notes, by_category, completed_this_week,
        active_projects, stale_projects and new_notes_this_week.
    """
    stale_days = int(settings.NOTIFY_STALE_DAYS)
    async with get_connection() as db:
        total = (await (await db.execute("SELECT COUNT(*) AS c FROM notes")).fetchone())["c"]

        rows = await (await db.execute(
            "SELECT para_category, COUNT(*) AS c FROM notes GROUP BY para_category")).fetchall()
        by_category = {r["para_category"]: r["c"] for r in rows}

        completed_rows = await (await db.execute(
            """SELECT id, title FROM notes
               WHERE status IN ('completed', 'archived')
                 AND updated_at >= datetime('now', '-7 days')
               ORDER BY updated_at DESC""")).fetchall()
        completed = [{"id": r["id"], "title": r["title"]} for r in completed_rows]

        active_rows = await (await db.execute(
            """SELECT id, title, deadline FROM notes
               WHERE para_category = 'projects' AND status = 'active'
               ORDER BY deadline ASC""")).fetchall()
        active = [{"id": r["id"], "title": r["title"], "deadline": r["deadline"]} for r in active_rows]

        stale_rows = await (await db.execute(
            f"""SELECT id, title FROM notes
                WHERE para_category = 'projects' AND status = 'active'
                  AND updated_at < datetime('now', '-{stale_days} days')
                ORDER BY updated_at ASC""")).fetchall()
        stale = [{"id": r["id"], "title": r["title"]} for r in stale_rows]

        new_rows = await (await db.execute(
            """SELECT id, title FROM notes
               WHERE created_at >= datetime('now', '-7 days')
               ORDER BY created_at DESC""")).fetchall()
        new_notes = [{"id": r["id"], "title": r["title"]} for r in new_rows]

        return {
            "total_notes": total,
            "by_category": by_category,
            "completed_this_week": completed,
            "active_projects": active,
            "stale_projects": stale,
            "new_notes_this_week": new_notes,
        }


@mcp.tool()
async def para_add_link(from_id: int, to_id: int, link_type: str = "related") -> dict:
    """Create a link between two notes.

    Args:
        from_id: Source note ID
        to_id: Target note ID
        link_type: related | depends_on | refines (default related)

    Returns:
        The created link, or ``{"error": ...}`` on invalid input.
    """
    if link_type not in LINK_TYPES:
        return {"error": f"Invalid link_type '{link_type}'. Must be one of: {', '.join(sorted(LINK_TYPES))}"}
    if from_id == to_id:
        return {"error": "Cannot link a note to itself"}
    async with get_connection() as db:
        for note_id in (from_id, to_id):
            if await _fetch_note(db, note_id) is None:
                return {"error": f"Note {note_id} not found"}
        cursor = await db.execute(
            "INSERT INTO links (from_note_id, to_note_id, link_type) VALUES (?, ?, ?)",
            (from_id, to_id, link_type),
        )
        link_id = cursor.lastrowid
        await db.commit()
        row = await (await db.execute("SELECT * FROM links WHERE id = ?", (link_id,))).fetchone()
        return dict(row)


@mcp.tool()
async def para_update(id: int, title: str | None = None, content: str | None = None,
                      priority: str | None = None, deadline: str | None = None,
                      tags: list[str] | None = None) -> dict:
    """Update note fields (any combination).

    Args:
        id: Note ID
        title: New title (optional)
        content: New content (optional)
        priority: New priority (optional)
        deadline: New deadline as ISO date string (optional)
        tags: New tags list (optional)

    Returns:
        The updated note, or ``{"error": ...}`` if not found.
    """
    async with get_connection() as db:
        existing = await _fetch_note(db, id)
        if existing is None:
            return {"error": f"Note {id} not found"}

        updates = []
        params = []
        changes = {}

        if title is not None:
            updates.append("title = ?")
            params.append(title)
            changes["title"] = (existing.get("title"), title)

        if content is not None:
            updates.append("content = ?")
            params.append(content)
            changes["content"] = (existing.get("content"), content)

        if priority is not None:
            updates.append("priority = ?")
            params.append(priority)
            changes["priority"] = (existing.get("priority"), priority)

        if deadline is not None:
            updates.append("deadline = ?")
            params.append(deadline)
            changes["deadline"] = (existing.get("deadline"), deadline)

        if tags is not None:
            tags_json = json.dumps(tags, ensure_ascii=False)
            updates.append("tags = ?")
            params.append(tags_json)
            changes["tags"] = (existing.get("tags"), tags)

        if not updates:
            return existing

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(id)

        sql = f"UPDATE notes SET {', '.join(updates)} WHERE id = ?"
        await db.execute(sql, params)

        for field, (old_val, new_val) in changes.items():
            await _log_history(db, id, "updated", old_value=str(old_val), new_value=str(new_val), reason=field)

        await db.commit()
        return await _fetch_note(db, id)


@mcp.tool()
async def para_complete(id: int) -> dict:
    """Mark a note as completed. If the note has a recurrence config, the next
    instance is automatically created with the computed next deadline.

    Args:
        id: Note ID

    Returns:
        The updated note with status='completed', or ``{"error": ...}`` if not found.
        Includes ``next_instance_id`` when a recurring note was spawned.
    """
    async with get_connection() as db:
        existing = await _fetch_note(db, id)
        if existing is None:
            return {"error": f"Note {id} not found"}

        await db.execute(
            "UPDATE notes SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (id,),
        )
        await _log_history(db, id, "completed", new_value="completed")

        next_id = await spawn_recurring_instance(db, existing)

        await db.commit()

        try:
            await emit_event(db, "note.completed", id, {
                "title": existing["title"],
                "status": "completed",
            })
        except Exception:
            logger.warning("Failed to emit note.completed for note %d", id, exc_info=True)

        result = await _fetch_note(db, id)
        if next_id is not None:
            result["next_instance_id"] = next_id
        return result


@mcp.tool()
async def para_delete(id: int) -> dict:
    """Soft delete a note (mark archived_at = NOW, don't remove row).

    Also deletes associated embeddings.

    Args:
        id: Note ID

    Returns:
        ``{"deleted": id}``, or ``{"error": ...}`` if not found.
    """
    async with get_connection() as db:
        existing = await _fetch_note(db, id)
        if existing is None:
            return {"error": f"Note {id} not found"}

        await db.execute(
            "UPDATE notes SET archived_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (id,),
        )
        await _log_history(db, id, "deleted")

        try:
            await delete_note_embedding(db, id)
        except Exception as e:
            logger.warning("Failed to delete embedding for note %d: %s", id, e)

        await db.commit()
        return {"deleted": id}


@mcp.tool()
async def para_reclassify(id: int) -> dict:
    """Fetch note, re-run classifier on title+content, update classification fields.

    Updates para_category, priority, tags, llm_confidence, llm_reasoning.

    Args:
        id: Note ID

    Returns:
        The updated note with new classification, or ``{"error": ...}`` if not found.
    """
    async with get_connection() as db:
        existing = await _fetch_note(db, id)
        if existing is None:
            return {"error": f"Note {id} not found"}

        result = await classify_note(existing["title"], existing["content"])
        para_category = result.get("para_category", "inbox")
        sub_category = result.get("sub_category")
        priority = result.get("priority", "medium")
        deadline = result.get("deadline")
        tags = result.get("tags", [])
        llm_model = result.get("llm_model")
        llm_confidence = float(result.get("confidence", 0.0))
        llm_reasoning = result.get("reasoning")

        await db.execute(
            """UPDATE notes SET para_category = ?, sub_category = ?, priority = ?,
               deadline = ?, tags = ?, llm_model = ?, llm_confidence = ?, llm_reasoning = ?,
               updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (para_category, sub_category, priority, deadline, json.dumps(tags, ensure_ascii=False),
             llm_model, llm_confidence, llm_reasoning, id),
        )

        await _log_history(db, id, "reclassified", new_value=para_category, reason=llm_reasoning)
        await db.commit()
        return await _fetch_note(db, id)


@mcp.tool()
async def para_ask(question: str) -> dict:
    """Ask a question across all PARA notes via RAG (semantic + keyword hybrid search).

    Finds relevant notes using hybrid retrieval, generates an answer grounded in
    those notes via the LLM, and returns the answer with cited source notes.

    Args:
        question: Natural language question

    Returns:
        {
            "answer": "...",
            "sources": [{"note_id", "title", "relevance", "para_category"}, ...]
        }
    """
    from app.chat import _hybrid_retrieve
    async with get_connection() as db:
        matched = await _hybrid_retrieve(question, db)

        if not matched:
            return {
                "answer": "ไม่พบโน้ตที่เกี่ยวข้องกับคำถามของคุณ",
                "sources": [],
            }

        context_lines = [
            f"- #{row['id']} [{row['para_category']}] {row['title']}: {(row['content'] or '')[:200]}"
            for row in matched
        ]
        messages = [
            {"role": "system", "content": settings.CHAT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"คำถาม: {question}\n\n"
                    f"โน้ตที่เกี่ยวข้อง:\n" + "\n".join(context_lines)
                ),
            },
        ]

        try:
            answer = await classifier.call_ollama(
                settings.CHAT_MODEL, messages=messages, format=None, task="ask"
            )
            answer = (answer or "").strip()
        except Exception as e:
            logger.error("para_ask LLM call failed: %s", e)
            answer = "ขออภัย เกิดข้อผิดพลาดขณะสร้างคำตอบ"

        total = max(len(matched), 1)
        sources = [
            {
                "note_id": row["id"],
                "title": row["title"],
                "relevance": round(1.0 - (idx / total), 3),
                "para_category": row["para_category"],
            }
            for idx, row in enumerate(matched)
        ]

        return {"answer": answer, "sources": sources}


async def _health_check(request: Request) -> JSONResponse:
    """Health check endpoint for the MCP HTTP server."""
    return JSONResponse({"status": "ok"})


def create_app() -> Starlette:
    """Create and return the Starlette application with SSE transport."""
    from mcp.server.sse import SseServerTransport

    sse = SseServerTransport("/mcp/messages")

    async def handle_sse(request: Request):
        async with sse.connect_sse(
            request.scope,
            request.receive,
            request._send,
        ) as session:
            await mcp._mcp_server.run(
                session._read_stream,
                session._write_stream,
                session._create_initialization_options(),
            )

    async def handle_messages(request: Request):
        await sse.handle_post_message(request.scope, request.receive, request._send)

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        ),
    ]

    routes = [
        Route("/mcp/sse", endpoint=handle_sse),
        Route("/mcp/messages", endpoint=handle_messages, methods=["POST"]),
        Route("/health", endpoint=_health_check),
    ]

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await init_db()
        yield

    return Starlette(
        debug=False,
        middleware=middleware,
        routes=routes,
        lifespan=lifespan,
    )


def main() -> None:
    """Initialize the database and run the MCP HTTP SSE server."""
    import uvicorn

    para_db = os.environ.get("PARA_DB")
    if para_db:
        settings.PARA_DB_PATH = para_db

    port = int(os.environ.get("PARA_MCP_HTTP_PORT", "8100"))
    app = create_app()
    logger.info("Starting MCP HTTP SSE server on port %d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
