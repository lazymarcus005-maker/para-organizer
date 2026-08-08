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

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from app import classifier
from app.classifier import classify_note, extract_deadline_from_text
from app.config import settings
from app.database import init_db
from app.database_v2 import async_session_factory
from app.distill import DISTILL_SYSTEM_PROMPT
from app.embed import embed_text
from app.events import emit_event
from app.models import PARA_CATEGORIES
from app.models_v2 import History, Link, Note
from app.utils import compute_next_deadline
from app.vector_store import delete_note_embedding, semantic_search

logger = logging.getLogger("para.mcp.http")

LINK_TYPES = {"related", "depends_on", "refines"}

mcp = FastMCP("para-organizer-http")


# ── Shared helpers ───────────────────────────────────────────────────────────

async def _log_history(session: AsyncSession, note_id: int, action: str, old_value: str | None = None,
                       new_value: str | None = None, reason: str | None = None) -> None:
    session.add(History(
        note_id=note_id, action=action,
        old_value=old_value, new_value=new_value, reason=reason,
    ))


def _note_to_dict(note: Note) -> dict:
    confidence = float(note.llm_confidence or 0.0)
    return {
        "id": note.id,
        "review_needed": note.llm_model is not None and confidence < settings.RECLASSIFY_CONFIDENCE_THRESHOLD,
        "title": note.title,
        "content": note.content,
        "para_category": note.para_category,
        "sub_category": note.sub_category,
        "status": note.status,
        "priority": note.priority,
        "deadline": note.deadline.isoformat() if note.deadline else None,
        "tags": note.tags if isinstance(note.tags, list) else [],
        "source": note.source,
        "source_metadata": note.source_metadata or {},
        "llm_model": note.llm_model,
        "llm_confidence": note.llm_confidence,
        "llm_reasoning": note.llm_reasoning,
        "embedding_status": note.embedding_status,
        "recurrence": note.recurrence,
        "created_at": note.created_at.isoformat() if note.created_at else None,
        "updated_at": note.updated_at.isoformat() if note.updated_at else None,
        "archived_at": note.archived_at.isoformat() if note.archived_at else None,
        "summary": note.summary,
    }


async def _fetch_note(session: AsyncSession, note_id: int) -> dict | None:
    # select() rather than session.get(): get() short-circuits on the identity
    # map and hands back the instance without emitting SQL, so attributes the
    # last UPDATE expired (updated_at, via its onupdate=func.now()) are still
    # unloaded. Reading one then triggers a lazy load outside the async
    # greenlet -> MissingGreenlet. A select() re-reads the row and repopulates
    # the expired attributes inside the awaited call. Same pattern as
    # app/routes_v2.py::_fetch_note.
    note = (await session.execute(select(Note).where(Note.id == note_id))).scalar_one_or_none()
    return _note_to_dict(note) if note is not None else None


# ── PostgreSQL-native replicas of SQLite-only helper modules ────────────────
#
# app.chat._hybrid_retrieve / app.distill / app.utils.spawn_recurring_instance
# still operate on aiosqlite connections and are also used by the legacy
# SQLite routers in app/routes/* (still mounted in app/main.py). Rather than
# changing their signatures (which would break those routers), the tools
# below replicate the minimal logic needed against PostgreSQL / SQLAlchemy.

async def _pg_distill_note(session: AsyncSession, note_id: int) -> str | None:
    note = await session.get(Note, note_id)
    if note is None:
        logger.warning("Note %d not found for distillation", note_id)
        return None
    messages = [
        {"role": "system", "content": DISTILL_SYSTEM_PROMPT},
        {"role": "user", "content": f"ชื่อ: {note.title}\n\nเนื้อหา:\n{note.content}"},
    ]
    try:
        summary = await classifier.call_ollama(settings.CHAT_MODEL, messages=messages, format=None, task="distill")
        return summary.strip() if summary else None
    except Exception as e:
        logger.warning("Failed to distill note %d: %s", note_id, e)
        return None


async def _pg_spawn_recurring_instance(session: AsyncSession, note: Note) -> int | None:
    """If `note` has a valid recurrence config, create the next instance."""
    if not isinstance(note.recurrence, dict) or not note.recurrence:
        return None
    recurrence = note.recurrence
    current_deadline = note.deadline.isoformat() if note.deadline else date.today().isoformat()
    next_deadline = compute_next_deadline(current_deadline, recurrence)
    if not next_deadline:
        return None

    new_note = Note(
        title=note.title, content=note.content, para_category=note.para_category,
        sub_category=note.sub_category, priority=note.priority,
        deadline=date.fromisoformat(next_deadline), tags=note.tags or [],
        source="recurring", recurrence=recurrence,
        llm_model=note.llm_model, llm_confidence=note.llm_confidence or 0.0,
        llm_reasoning=note.llm_reasoning,
    )
    session.add(new_note)
    await session.flush()
    session.add(History(
        note_id=new_note.id, action="created", new_value="recurring",
        reason=f"Recurring instance from #{note.id}",
    ))
    logger.info("Spawned recurring instance #%s from #%s, deadline %s", new_note.id, note.id, next_deadline)
    return new_note.id


async def _pg_hybrid_retrieve(session: AsyncSession, user_text: str) -> list[dict]:
    """Merge keyword (tsvector) and semantic (pgvector) search into one ranked
    list of notes, weighted by RAG_HYBRID_RATIO."""
    scores: dict[int, dict[str, float]] = {}

    if settings.RAG_HYBRID_ENABLED:
        try:
            query_embedding = await embed_text(user_text)
            if query_embedding:
                for note_id, score in await semantic_search(session, query_embedding, limit=settings.RAG_SEARCH_LIMIT):
                    scores.setdefault(note_id, {})["semantic"] = score
        except Exception:
            logger.warning("Semantic search failed, falling back to FTS only", exc_info=True)

    try:
        query_ts = func.plainto_tsquery("simple", user_text)
        stmt = (
            select(Note.id)
            .where(Note.search_vector.op("@@")(query_ts))
            .order_by(func.ts_rank(Note.search_vector, query_ts).desc())
            .limit(settings.RAG_SEARCH_LIMIT)
        )
        fts_ids = [row[0] for row in (await session.execute(stmt)).all()]
        for rank, note_id in enumerate(fts_ids):
            scores.setdefault(note_id, {})["fts"] = 1.0 / (rank + 1.0)
    except Exception:
        logger.warning("Full-text search failed", exc_info=True)

    if not scores:
        return []

    ratio = settings.RAG_HYBRID_RATIO
    for note_scores in scores.values():
        note_scores["combined"] = (
            ratio * note_scores.get("semantic", 0.0) + (1 - ratio) * note_scores.get("fts", 0.0)
        )

    top_ids = sorted(scores, key=lambda nid: scores[nid]["combined"], reverse=True)[:settings.RAG_SEARCH_LIMIT]
    if not top_ids:
        return []

    rows = (await session.execute(select(Note).where(Note.id.in_(top_ids)))).scalars().all()
    by_id = {n.id: n for n in rows}
    return [
        {"id": by_id[nid].id, "title": by_id[nid].title, "content": by_id[nid].content,
         "para_category": by_id[nid].para_category}
        for nid in top_ids if nid in by_id
    ]


# ── Tools ─────────────────────────────────────────────────────────────────────

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

    async with async_session_factory() as session:
        note = Note(
            title=title, content=content, para_category=para_category, sub_category=sub_category,
            priority=priority, deadline=date.fromisoformat(deadline) if deadline else None,
            tags=tags, source="hermes", llm_model=llm_model, llm_confidence=llm_confidence,
            llm_reasoning=llm_reasoning,
        )
        session.add(note)
        await session.flush()
        note_id = note.id
        await _log_history(session, note_id, "created", new_value="hermes")
        await _log_history(session, note_id, "classified", new_value=para_category, reason=llm_reasoning)
        await session.commit()
        return await _fetch_note(session, note_id)


@mcp.tool()
async def para_search(query: str, category: str | None = None, limit: int = 10) -> list:
    """Full-text search over notes (PostgreSQL tsvector).

    Args:
        query: Search query (matches title, content, tags)
        category: Optional PARA category filter
        limit: Maximum number of results (default 10)

    Returns:
        List of matching notes ranked by relevance (each includes a ``rank`` key),
        or ``{"error": ...}`` if the query is malformed.
    """
    async with async_session_factory() as session:
        try:
            query_ts = func.plainto_tsquery("simple", query)
            stmt = select(Note, func.ts_rank(Note.search_vector, query_ts).label("rank")).where(
                Note.search_vector.op("@@")(query_ts)
            )
            if category:
                stmt = stmt.where(Note.para_category == category)
            stmt = stmt.order_by(func.ts_rank(Note.search_vector, query_ts).desc()).limit(limit)
            rows = (await session.execute(stmt)).all()
        except Exception as e:
            logger.warning("para_search failed for query %r: %s", query, e)
            return {"error": f"Search failed: {e}"}

        results = []
        for note, rank in rows:
            d = _note_to_dict(note)
            d["rank"] = float(rank) if rank is not None else 0.0
            results.append(d)
        return results


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
    async with async_session_factory() as session:
        stmt = select(Note)
        if category:
            stmt = stmt.where(Note.para_category == category)
        if status:
            stmt = stmt.where(Note.status == status)
        stmt = stmt.order_by(Note.created_at.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
        return [_note_to_dict(n) for n in rows]


@mcp.tool()
async def para_get(id: int) -> dict:
    """Get a single note by its ID.

    Args:
        id: Note ID

    Returns:
        The note, or ``{"error": ...}`` if not found.
    """
    async with async_session_factory() as session:
        note = await _fetch_note(session, id)
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
    async with async_session_factory() as session:
        existing = await _fetch_note(session, id)
        if existing is None:
            return {"error": f"Note {id} not found"}
        note = await session.get(Note, id)
        note.para_category = category
        await _log_history(session, id, "moved", old_value=existing["para_category"], new_value=category)
        await session.commit()
        return await _fetch_note(session, id)


@mcp.tool()
async def para_archive(id: int) -> dict:
    """Archive a note (moves it to archives and marks status archived).

    Args:
        id: Note ID

    Returns:
        The updated note, or ``{"error": ...}`` if not found.
    """
    async with async_session_factory() as session:
        existing = await _fetch_note(session, id)
        if existing is None:
            return {"error": f"Note {id} not found"}

        note = await session.get(Note, id)

        summary = await _pg_distill_note(session, id)
        note.para_category = "archives"
        note.status = "archived"
        note.summary = summary
        note.archived_at = datetime.now(timezone.utc)
        await _log_history(session, id, "archived", old_value=existing["para_category"], new_value="archives")

        summary = await _pg_distill_note(session, id)
        if summary:
            note.summary = summary
            await _log_history(session, id, "distilled", new_value=summary)

        await session.commit()

        try:
            await emit_event(None, "note.completed", id, {
                "title": existing["title"],
                "para_category": "archives",
                "status": "archived",
            })
        except Exception:
            logger.warning("Failed to emit note.completed for note %d", id, exc_info=True)

        return await _fetch_note(session, id)


@mcp.tool()
async def para_stats() -> dict:
    """Summary statistics for the whole PARA system.

    Returns:
        Dict with total_notes, by_category, by_status, by_priority,
        upcoming_deadlines and avg_confidence.
    """
    async with async_session_factory() as session:
        total = (await session.execute(select(func.count()).select_from(Note))).scalar_one()

        rows = (await session.execute(select(Note.para_category, func.count()).group_by(Note.para_category))).all()
        by_category = {r[0]: r[1] for r in rows}

        rows = (await session.execute(select(Note.status, func.count()).group_by(Note.status))).all()
        by_status = {r[0]: r[1] for r in rows}

        rows = (await session.execute(select(Note.priority, func.count()).group_by(Note.priority))).all()
        by_priority = {r[0]: r[1] for r in rows}

        today = date.today()
        upcoming = (await session.execute(
            select(func.count()).select_from(Note).where(
                Note.deadline.isnot(None), Note.status == "active", Note.deadline >= today,
            )
        )).scalar_one()

        avg_conf = (await session.execute(select(func.avg(Note.llm_confidence)))).scalar_one() or 0.0

        return {
            "total_notes": total,
            "by_category": by_category,
            "by_status": by_status,
            "by_priority": by_priority,
            "upcoming_deadlines": upcoming,
            "avg_confidence": round(float(avg_conf), 3),
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
    horizon = today + timedelta(days=days_ahead)
    async with async_session_factory() as session:
        rows = (await session.execute(
            select(Note.id, Note.title, Note.deadline, Note.priority).where(
                Note.deadline.isnot(None), Note.status == "active",
                Note.deadline >= today, Note.deadline <= horizon,
            ).order_by(Note.deadline.asc())
        )).all()
        return [
            {
                "id": r.id, "title": r.title, "deadline": r.deadline.isoformat(),
                "days_left": (r.deadline - today).days, "priority": r.priority,
            }
            for r in rows
        ]


@mcp.tool()
async def para_digest() -> dict:
    """Weekly digest: totals, completed this week, active and stale projects, new notes.

    Returns:
        Digest dict with total_notes, by_category, completed_this_week,
        active_projects, stale_projects and new_notes_this_week.
    """
    stale_days = int(settings.NOTIFY_STALE_DAYS)
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    stale_cutoff = now - timedelta(days=stale_days)

    async with async_session_factory() as session:
        total = (await session.execute(select(func.count()).select_from(Note))).scalar_one()

        rows = (await session.execute(select(Note.para_category, func.count()).group_by(Note.para_category))).all()
        by_category = {r[0]: r[1] for r in rows}

        completed_rows = (await session.execute(
            select(Note.id, Note.title).where(
                Note.status.in_(["completed", "archived"]), Note.updated_at >= week_ago,
            ).order_by(Note.updated_at.desc())
        )).all()
        completed = [{"id": r.id, "title": r.title} for r in completed_rows]

        active_rows = (await session.execute(
            select(Note.id, Note.title, Note.deadline).where(
                Note.para_category == "projects", Note.status == "active",
            ).order_by(Note.deadline.asc())
        )).all()
        active = [
            {"id": r.id, "title": r.title, "deadline": r.deadline.isoformat() if r.deadline else None}
            for r in active_rows
        ]

        stale_rows = (await session.execute(
            select(Note.id, Note.title).where(
                Note.para_category == "projects", Note.status == "active", Note.updated_at < stale_cutoff,
            ).order_by(Note.updated_at.asc())
        )).all()
        stale = [{"id": r.id, "title": r.title} for r in stale_rows]

        new_rows = (await session.execute(
            select(Note.id, Note.title).where(Note.created_at >= week_ago).order_by(Note.created_at.desc())
        )).all()
        new_notes = [{"id": r.id, "title": r.title} for r in new_rows]

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
    async with async_session_factory() as session:
        for note_id in (from_id, to_id):
            if await _fetch_note(session, note_id) is None:
                return {"error": f"Note {note_id} not found"}
        link = Link(from_note_id=from_id, to_note_id=to_id, link_type=link_type)
        session.add(link)
        await session.commit()
        await session.refresh(link)
        return {
            "id": link.id,
            "from_note_id": link.from_note_id,
            "to_note_id": link.to_note_id,
            "link_type": link.link_type,
            "created_at": link.created_at.isoformat() if link.created_at else None,
        }


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
    async with async_session_factory() as session:
        existing = await _fetch_note(session, id)
        if existing is None:
            return {"error": f"Note {id} not found"}

        note = await session.get(Note, id)
        changes: dict[str, tuple] = {}

        if title is not None:
            changes["title"] = (existing.get("title"), title)
            note.title = title

        if content is not None:
            changes["content"] = (existing.get("content"), content)
            note.content = content

        if priority is not None:
            changes["priority"] = (existing.get("priority"), priority)
            note.priority = priority

        if deadline is not None:
            changes["deadline"] = (existing.get("deadline"), deadline)
            note.deadline = date.fromisoformat(deadline)

        if tags is not None:
            changes["tags"] = (existing.get("tags"), tags)
            note.tags = tags

        if not changes:
            return existing

        for field, (old_val, new_val) in changes.items():
            await _log_history(session, id, "updated", old_value=str(old_val), new_value=str(new_val), reason=field)

        await session.commit()
        return await _fetch_note(session, id)


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
    async with async_session_factory() as session:
        existing = await _fetch_note(session, id)
        if existing is None:
            return {"error": f"Note {id} not found"}

        note = await session.get(Note, id)
        note.status = "completed"
        await _log_history(session, id, "completed", new_value="completed")

        next_id = await _pg_spawn_recurring_instance(session, note)

        await session.commit()

        try:
            await emit_event(None, "note.completed", id, {
                "title": existing["title"],
                "status": "completed",
            })
        except Exception:
            logger.warning("Failed to emit note.completed for note %d", id, exc_info=True)

        result = await _fetch_note(session, id)
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
    async with async_session_factory() as session:
        existing = await _fetch_note(session, id)
        if existing is None:
            return {"error": f"Note {id} not found"}

        note = await session.get(Note, id)
        note.archived_at = datetime.now(timezone.utc)
        await _log_history(session, id, "deleted")

        try:
            await delete_note_embedding(session, id)
        except Exception as e:
            logger.warning("Failed to delete embedding for note %d: %s", id, e)

        await session.commit()
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
    async with async_session_factory() as session:
        existing = await _fetch_note(session, id)
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

        note = await session.get(Note, id)
        note.para_category = para_category
        note.sub_category = sub_category
        note.priority = priority
        note.deadline = date.fromisoformat(deadline) if deadline else None
        note.tags = tags
        note.llm_model = llm_model
        note.llm_confidence = llm_confidence
        note.llm_reasoning = llm_reasoning

        await _log_history(session, id, "reclassified", new_value=para_category, reason=llm_reasoning)
        await session.commit()
        return await _fetch_note(session, id)


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
    async with async_session_factory() as session:
        matched = await _pg_hybrid_retrieve(session, question)

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
