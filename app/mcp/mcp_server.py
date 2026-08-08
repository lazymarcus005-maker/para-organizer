"""MCP server exposing PARA Organizer tools to Hermes (stdio transport).

Launched by Hermes as a subprocess:

    python3 app/mcp/mcp_server.py

Hermes config (~/.hermes/config.yaml):

    mcp:
      servers:
        para-organizer:
          command: python3
          args: ["app/mcp/mcp_server.py"]
          env:
            PARA_DB: /var/lib/para-organizer/data/para.db
            OLLAMA_API_KEY: ${OLLAMA_API_KEY}

Each tool talks to PostgreSQL via async SQLAlchemy sessions (app.database_v2 /
app.models_v2) and returns JSON-serializable dicts/lists. Expected errors
(not found, invalid input) are returned as ``{"error": ...}`` dicts so the
server never crashes.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import asyncio
import logging
from collections import deque
from datetime import date, datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import classifier
from app.classifier import classify_note, extract_deadline_from_text
from app.config import settings
from app.database import init_db
from app.database_v2 import async_session_factory
from app.distill import DISTILL_SYSTEM_PROMPT
from app.embed import embed_text
from app.events import emit_event
from app.feedback import get_feedback_stats
from app.items import compute_progress, create_item, list_items, sync_note_progress, update_item
from app.models import PARA_CATEGORIES
from app.models_v2 import Event, History, Item, Link, Note, Task
from app.planner import _suggest_focus, _urgency_key
from app.utils import compute_next_deadline
from app.vector_store import delete_note_embedding, semantic_search

logger = logging.getLogger("para.mcp")

LINK_TYPES = {"related", "depends_on", "refines"}

mcp = FastMCP("para-organizer")


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


def _task_to_dict(task: Task) -> dict:
    return {
        "id": task.id,
        "prompt": task.prompt,
        "status": task.status,
        "note_id": task.note_id,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


# ── PostgreSQL-native replicas of SQLite-only helper modules ────────────────
#
# app.graph / app.context / app.chat._hybrid_retrieve / app.distill /
# app.tasks / app.utils.spawn_recurring_instance still operate on
# aiosqlite connections and are also used by the legacy SQLite routers in
# app/routes/* (still mounted in app/main.py). Rather than changing their
# signatures (which would break those routers), the MCP tools below
# replicate the minimal logic needed against PostgreSQL / SQLAlchemy.

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


async def _pg_get_subgraph(session: AsyncSession, note_id: int, depth: int = 2) -> dict:
    root = await session.get(Note, note_id)
    if root is None:
        return {"root": None, "nodes": [], "edges": [], "depth": depth, "node_count": 0, "edge_count": 0}

    visited: dict[int, int] = {note_id: 0}
    edges: list[dict] = []
    seen_edges: set[tuple[int, int]] = set()
    frontier: deque[int] = deque([note_id])

    while frontier:
        current = frontier.popleft()
        current_depth = visited[current]
        if current_depth >= depth:
            continue

        rows = (await session.execute(
            select(Link).where((Link.from_note_id == current) | (Link.to_note_id == current))
        )).scalars().all()

        for link in rows:
            neighbor = link.to_note_id if link.from_note_id == current else link.from_note_id
            edge_key = (link.from_note_id, link.to_note_id)
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                edges.append({"from_id": link.from_note_id, "to_id": link.to_note_id, "link_type": link.link_type})
            if neighbor not in visited:
                visited[neighbor] = current_depth + 1
                frontier.append(neighbor)

    node_ids = list(visited.keys())
    nodes = []
    if node_ids:
        rows = (await session.execute(select(Note).where(Note.id.in_(node_ids)))).scalars().all()
        for n in rows:
            nodes.append({
                "id": n.id, "title": n.title, "para_category": n.para_category,
                "status": n.status, "depth": visited[n.id],
            })

    return {
        "root": {"id": root.id, "title": root.title, "para_category": root.para_category},
        "nodes": nodes,
        "edges": edges,
        "depth": depth,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


async def _pg_get_related(session: AsyncSession, note_id: int, limit: int = 10) -> list[dict]:
    results: list[dict] = []
    seen_ids: set[int] = {note_id}

    rows = (await session.execute(
        select(Link).where((Link.from_note_id == note_id) | (Link.to_note_id == note_id))
    )).scalars().all()

    linked_ids: list[tuple[int, str]] = []
    for link in rows:
        neighbor = link.to_note_id if link.from_note_id == note_id else link.from_note_id
        if neighbor not in seen_ids:
            seen_ids.add(neighbor)
            linked_ids.append((neighbor, link.link_type))

    if linked_ids:
        ids_only = [nid for nid, _ in linked_ids]
        note_rows = (await session.execute(select(Note).where(Note.id.in_(ids_only)))).scalars().all()
        note_map = {n.id: n for n in note_rows}
        link_type_map = dict(linked_ids)
        for nid, _ in linked_ids:
            if nid in note_map and len(results) < limit:
                n = note_map[nid]
                results.append({
                    "id": n.id, "title": n.title, "para_category": n.para_category,
                    "relation": "linked", "link_type": link_type_map.get(nid),
                })

    if len(results) < limit:
        try:
            note = await session.get(Note, note_id)
            if note is not None:
                embedding = await embed_text(f"{note.title} {note.content}")
                if embedding:
                    sem_results = await semantic_search(session, embedding, limit=limit)
                    for sem_id, _score in sem_results:
                        if sem_id not in seen_ids and len(results) < limit:
                            sr = await session.get(Note, sem_id)
                            if sr is not None:
                                seen_ids.add(sem_id)
                                results.append({
                                    "id": sr.id, "title": sr.title, "para_category": sr.para_category,
                                    "relation": "semantic", "link_type": None,
                                })
        except Exception:
            logger.warning("Semantic related search failed for note %s, using links only", note_id, exc_info=True)

    return results


async def _pg_graph_neighbors(session: AsyncSession, note_ids: list[int]) -> list[dict]:
    if not note_ids:
        return []
    rows = (await session.execute(
        select(Link).where((Link.from_note_id.in_(note_ids)) | (Link.to_note_id.in_(note_ids)))
    )).scalars().all()

    seen = set(note_ids)
    candidates: list[tuple[int, str]] = []
    for link in rows:
        if link.from_note_id in note_ids and link.to_note_id not in seen:
            candidates.append((link.to_note_id, link.link_type))
        if link.to_note_id in note_ids and link.from_note_id not in seen:
            candidates.append((link.from_note_id, link.link_type))

    neighbors: list[dict] = []
    for nid, link_type in candidates:
        if nid in seen:
            continue
        seen.add(nid)
        note = await session.get(Note, nid)
        if note is not None:
            neighbors.append({"id": note.id, "title": note.title, "link_type": link_type})
    return neighbors


async def _pg_build_context(session: AsyncSession, topic: str, limit: int = 5) -> dict:
    logger.info("Building context for topic %r (limit=%s)", topic, limit)
    context: dict = {
        "topic": topic,
        "related_notes": [],
        "upcoming_deadlines": [],
        "pending_tasks": [],
        "recent_activity": [],
        "graph_neighbors": [],
        "quick_stats": {},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        matched = await _pg_hybrid_retrieve(session, topic)
        context["related_notes"] = [
            {
                "id": row["id"], "title": row["title"], "para_category": row["para_category"],
                "snippet": (row["content"] or "")[:200], "relevance": round(1.0 / (rank + 1.0), 4),
            }
            for rank, row in enumerate(matched[:limit])
        ]
    except Exception:
        logger.warning("related_notes retrieval failed", exc_info=True)

    try:
        today = date.today()
        horizon = today + timedelta(days=14)
        rows = (await session.execute(
            select(Note.id, Note.title, Note.deadline).where(
                Note.status == "active", Note.deadline.isnot(None),
                Note.deadline >= today, Note.deadline <= horizon,
            ).order_by(Note.deadline.asc()).limit(limit)
        )).all()
        context["upcoming_deadlines"] = [
            {
                "id": r.id, "title": r.title,
                "deadline": r.deadline.isoformat() if r.deadline else None,
                "days_left": (r.deadline - today).days if r.deadline else None,
            }
            for r in rows
        ]
    except Exception:
        logger.warning("upcoming_deadlines retrieval failed", exc_info=True)

    try:
        rows = (await session.execute(
            select(Task).where(Task.status.in_(["pending", "dispatched"]))
            .order_by(Task.created_at.desc()).limit(limit)
        )).scalars().all()
        context["pending_tasks"] = [
            {"id": t.id, "prompt": t.prompt, "status": t.status, "note_id": t.note_id} for t in rows
        ]
    except Exception:
        logger.warning("pending_tasks retrieval failed", exc_info=True)

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        rows = (await session.execute(
            select(Note.id, Note.title, Note.para_category, Note.updated_at)
            .where(Note.updated_at >= cutoff).order_by(Note.updated_at.desc()).limit(limit)
        )).all()
        context["recent_activity"] = [
            {
                "id": r.id, "title": r.title, "para_category": r.para_category,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
    except Exception:
        logger.warning("recent_activity retrieval failed", exc_info=True)

    try:
        related_ids = [note["id"] for note in context["related_notes"]]
        context["graph_neighbors"] = await _pg_graph_neighbors(session, related_ids)
    except Exception:
        logger.warning("graph_neighbors retrieval failed", exc_info=True)

    try:
        total = (await session.execute(select(func.count()).select_from(Note))).scalar_one()
        cat_rows = (await session.execute(
            select(Note.para_category, func.count()).group_by(Note.para_category)
        )).all()
        by_category = {r[0]: r[1] for r in cat_rows}
        context["quick_stats"] = {
            "total_notes": total, "by_category": by_category, "inbox_count": by_category.get("inbox", 0),
        }
    except Exception:
        logger.warning("quick_stats retrieval failed", exc_info=True)

    return context


async def _pg_generate_plan(session: AsyncSession, horizon_days: int = 7) -> dict:
    today = date.today()
    now = datetime.now(timezone.utc)
    horizon_date = today + timedelta(days=horizon_days)
    stale_cutoff = now - timedelta(days=settings.NOTIFY_STALE_DAYS)

    deadline_rows = (await session.execute(
        select(Note.id, Note.title, Note.priority, Note.deadline).where(
            Note.status == "active", Note.deadline.isnot(None),
            Note.deadline >= today, Note.deadline <= horizon_date,
        ).order_by(Note.deadline.asc())
    )).all()

    stale_rows = (await session.execute(
        select(Note.id, Note.title, Note.updated_at).where(
            Note.para_category == "projects", Note.status == "active", Note.updated_at < stale_cutoff,
        ).order_by(Note.updated_at.asc())
    )).all()

    # Notes with unfinished action items (no `progress` column on notes in PG;
    # derive "in progress" from the items table instead).
    progress_rows = (await session.execute(
        select(
            Note.id, Note.title, Note.priority, Note.deadline,
            func.count(Item.id).label("total"),
            func.coalesce(func.sum(case((Item.done.is_(True), 1), else_=0)), 0).label("done"),
        )
        .join(Item, Item.note_id == Note.id)
        .where(Note.status == "active")
        .group_by(Note.id, Note.title, Note.priority, Note.deadline)
        .having(func.count(Item.id) > func.coalesce(func.sum(case((Item.done.is_(True), 1), else_=0)), 0))
    )).all()

    prioritized: list[dict] = []
    seen: set[int] = set()

    for row in deadline_rows:
        days_left = (row.deadline - today).days if row.deadline else None
        prioritized.append({
            "note_id": row.id,
            "title": row.title,
            "reason": "deadline approaching",
            "priority": row.priority,
            "deadline": row.deadline.isoformat() if row.deadline else None,
            "days_left": days_left,
        })
        seen.add(row.id)

    for row in progress_rows:
        if row.id in seen:
            continue
        total = row.total or 0
        done = row.done or 0
        pct = round(done / total * 100) if total else 0
        days_left = (row.deadline - today).days if row.deadline else None
        prioritized.append({
            "note_id": row.id,
            "title": row.title,
            "reason": f"in progress, {pct}% done",
            "priority": row.priority,
            "deadline": row.deadline.isoformat() if row.deadline else None,
            "days_left": days_left,
        })
        seen.add(row.id)

    prioritized.sort(key=_urgency_key)

    stale_to_revisit: list[dict] = []
    for row in stale_rows:
        days_stale = (now - row.updated_at).days if row.updated_at else settings.NOTIFY_STALE_DAYS
        stale_to_revisit.append({"note_id": row.id, "title": row.title, "days_stale": days_stale})

    suggested_focus = await _suggest_focus(prioritized, stale_to_revisit)

    return {
        "horizon_days": horizon_days,
        "period": {"start": today.isoformat(), "end": horizon_date.isoformat()},
        "prioritized_actions": prioritized,
        "stale_to_revisit": stale_to_revisit,
        "suggested_focus": suggested_focus,
        "generated_at": now.isoformat(),
    }


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
        The updated note, or ``{\"error\": ...}`` if not found.
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
            # No fields to update
            return existing

        # Log history for each change
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
        The updated note with status='completed', or ``{\"error\": ...}`` if not found.
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
        ``{\"deleted\": id}``, or ``{\"error\": ...}`` if not found.
    """
    async with async_session_factory() as session:
        existing = await _fetch_note(session, id)
        if existing is None:
            return {"error": f"Note {id} not found"}

        note = await session.get(Note, id)
        note.archived_at = datetime.now(timezone.utc)
        await _log_history(session, id, "deleted")

        # Delete embeddings
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
        The updated note with new classification, or ``{\"error\": ...}`` if not found.
    """
    async with async_session_factory() as session:
        existing = await _fetch_note(session, id)
        if existing is None:
            return {"error": f"Note {id} not found"}

        # Re-classify
        result = await classify_note(existing["title"], existing["content"])
        para_category = result.get("para_category", "inbox")
        sub_category = result.get("sub_category")
        priority = result.get("priority", "medium")
        deadline = result.get("deadline")
        tags = result.get("tags", [])
        llm_model = result.get("llm_model")
        llm_confidence = float(result.get("confidence", 0.0))
        llm_reasoning = result.get("reasoning")

        # Update note with new classification
        note = await session.get(Note, id)
        note.para_category = para_category
        note.sub_category = sub_category
        note.priority = priority
        note.deadline = date.fromisoformat(deadline) if deadline else None
        note.tags = tags
        note.llm_model = llm_model
        note.llm_confidence = llm_confidence
        note.llm_reasoning = llm_reasoning

        # Log the reclassification
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


@mcp.tool()
async def para_context(topic: str, limit: int = 5) -> dict:
    """Build a situational context package for an agent about to work on a topic.

    Returns related notes (hybrid search), upcoming deadlines, pending tasks,
    recent activity, graph neighbors, and quick stats — everything an agent
    needs to understand the current state of the brain before acting.

    Args:
        topic: What the agent is about to work on
        limit: Max items per section (default 5)

    Returns:
        Context package dict with related_notes, upcoming_deadlines,
        pending_tasks, recent_activity, graph_neighbors, quick_stats.
    """
    async with async_session_factory() as session:
        return await _pg_build_context(session, topic, limit)


@mcp.tool()
async def para_create_task(note_id: int | None = None, prompt: str = "",
                           task_type: str = "general") -> dict:
    """Create a task to be delegated to Hermes (or another agent).

    Args:
        note_id: Optional source note ID the task relates to
        prompt: What the agent should do
        task_type: general | research | code | deploy | review | automation

    Returns:
        The created task dict, or ``{"error": ...}`` on invalid input.
    """
    if not prompt or not prompt.strip():
        return {"error": "prompt is required"}
    async with async_session_factory() as session:
        if note_id is not None and await _fetch_note(session, note_id) is None:
            return {"error": f"Note {note_id} not found"}
        task = Task(note_id=note_id, prompt=prompt.strip())
        session.add(task)
        await session.commit()
        await session.refresh(task)
        result = _task_to_dict(task)
        # `tasks` has no task_type column in PostgreSQL — echoed back, not persisted.
        result["task_type"] = task_type
        return result


@mcp.tool()
async def para_task_result(task_id: int, result: str) -> dict:
    """Report the result of a completed task back to PARA.

    Marks the task as completed and automatically creates a new note from
    the result (auto-classified by the LLM), closing the delegation loop.

    Args:
        task_id: Task ID
        result: What the agent accomplished / found

    Returns:
        The updated task dict, or ``{"error": ...}`` if not found.
    """
    async with async_session_factory() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return {"error": f"Task {task_id} not found"}

        task.status = "completed"

        note_id = None
        if task.note_id is not None:
            try:
                cls = await classify_note(f"Task #{task_id} result", result)
                para_category = cls.get("para_category", "inbox")
                sub_category = cls.get("sub_category")
                priority = cls.get("priority", "medium")
                dl = cls.get("deadline")
                deadline = date.fromisoformat(dl) if dl else None
                tags = cls.get("tags", [])
                llm_model = cls.get("llm_model")
                llm_confidence = float(cls.get("confidence", 0.0))
                llm_reasoning = cls.get("reasoning")

                new_note = Note(
                    title=f"Task #{task_id} result", content=result,
                    para_category=para_category, sub_category=sub_category,
                    priority=priority, deadline=deadline, tags=tags,
                    source=f"task:{task_id}", llm_model=llm_model,
                    llm_confidence=llm_confidence, llm_reasoning=llm_reasoning,
                )
                session.add(new_note)
                await session.flush()
                note_id = new_note.id
                await _log_history(session, note_id, "created", new_value=f"task:{task_id}")
                logger.info("Created note #%s from task #%s result", note_id, task_id)
            except Exception:
                logger.warning("Failed to create note from task #%s result", task_id, exc_info=True)

        await session.commit()
        await session.refresh(task)
        out = _task_to_dict(task)
        # `tasks` has no result column in PostgreSQL — echoed back, not persisted.
        out["result"] = result
        if note_id is not None:
            out["result_note_id"] = note_id
        return out


@mcp.tool()
async def para_tasks(status: str | None = None, limit: int = 20) -> dict:
    """List delegated tasks, optionally filtered by status.

    Args:
        status: Optional filter (pending | dispatched | completed | failed)
        limit: Max results (default 20)

    Returns:
        {"tasks": [...], "total": int}
    """
    async with async_session_factory() as session:
        stmt = select(Task)
        if status:
            stmt = stmt.where(Task.status == status)
        total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        stmt = stmt.order_by(Task.created_at.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
        return {"tasks": [_task_to_dict(t) for t in rows], "total": total}


@mcp.tool()
async def para_brain_state() -> dict:
    """Snapshot of the entire brain state — a single call giving Hermes the
    full picture: stats, active projects, deadlines, pending tasks, inbox
    items awaiting review, stale items, and recent events.

    Returns:
        Brain state dict combining stats, deadlines, tasks, inbox, stale,
        and recent events.
    """
    stale_days = int(settings.NOTIFY_STALE_DAYS)
    today = date.today()
    horizon = today + timedelta(days=14)
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=stale_days)

    async with async_session_factory() as session:
        total = (await session.execute(select(func.count()).select_from(Note))).scalar_one()
        cat_rows = (await session.execute(
            select(Note.para_category, func.count()).group_by(Note.para_category)
        )).all()
        by_category = {r[0]: r[1] for r in cat_rows}

        status_rows = (await session.execute(
            select(Note.status, func.count()).group_by(Note.status)
        )).all()
        by_status = {r[0]: r[1] for r in status_rows}

        deadlines_rows = (await session.execute(
            select(Note.id, Note.title, Note.deadline, Note.priority).where(
                Note.deadline.isnot(None), Note.status == "active",
                Note.deadline >= today, Note.deadline <= horizon,
            ).order_by(Note.deadline.asc())
        )).all()
        deadlines = [
            {
                "id": r.id, "title": r.title, "deadline": r.deadline.isoformat(),
                "days_left": (r.deadline - today).days, "priority": r.priority,
            }
            for r in deadlines_rows
        ]

        inbox_rows = (await session.execute(
            select(Note.id, Note.title, Note.llm_confidence).where(
                Note.para_category == "inbox", Note.status == "active",
            ).order_by(Note.created_at.desc()).limit(10)
        )).all()
        inbox = [{"id": r.id, "title": r.title, "llm_confidence": r.llm_confidence} for r in inbox_rows]

        stale_rows = (await session.execute(
            select(Note.id, Note.title, Note.updated_at).where(
                Note.para_category == "projects", Note.status == "active", Note.updated_at < stale_cutoff,
            ).order_by(Note.updated_at.asc())
        )).all()
        stale = [
            {"id": r.id, "title": r.title, "updated_at": r.updated_at.isoformat() if r.updated_at else None}
            for r in stale_rows
        ]

        try:
            task_rows = (await session.execute(
                select(Task.id, Task.prompt, Task.status).where(
                    Task.status.in_(["pending", "dispatched"])
                ).order_by(Task.created_at.desc()).limit(10)
            )).all()
            pending_tasks = [{"id": r.id, "prompt": r.prompt, "status": r.status} for r in task_rows]
        except Exception:
            pending_tasks = []

        try:
            event_rows = (await session.execute(
                select(Event.id, Event.event_type, Event.note_id, Event.status, Event.created_at)
                .order_by(Event.created_at.desc()).limit(10)
            )).all()
            recent_events = [
                {
                    "id": r.id, "event_type": r.event_type, "note_id": r.note_id, "status": r.status,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in event_rows
            ]
        except Exception:
            recent_events = []

    return {
        "stats": {
            "total_notes": total,
            "by_category": by_category,
            "by_status": by_status,
        },
        "upcoming_deadlines": deadlines,
        "inbox_awaiting_review": inbox,
        "stale_projects": stale,
        "pending_tasks": pending_tasks,
        "recent_events": recent_events,
    }


@mcp.tool()
async def para_graph_context(note_id: int, depth: int = 2) -> dict:
    """Get the knowledge subgraph around a note — all connected notes up to N hops.

    Args:
        note_id: Center note ID
        depth: How many hops to traverse (default 2)

    Returns:
        Subgraph with nodes, edges, and counts, or ``{"error": ...}`` if not found.
    """
    async with async_session_factory() as session:
        if await _fetch_note(session, note_id) is None:
            return {"error": f"Note {note_id} not found"}
        return await _pg_get_subgraph(session, note_id, depth)


@mcp.tool()
async def para_related(note_id: int, limit: int = 10) -> list:
    """Find notes related to a given note — via explicit links and semantic similarity.

    Args:
        note_id: Note ID
        limit: Max results (default 10)

    Returns:
        List of related notes with relation type, or ``{"error": ...}`` if not found.
    """
    async with async_session_factory() as session:
        if await _fetch_note(session, note_id) is None:
            return {"error": f"Note {note_id} not found"}
        return await _pg_get_related(session, note_id, limit)


@mcp.tool()
async def para_items(note_id: int) -> dict:
    """List action items (subtasks) for a note, with progress percentage.

    Args:
        note_id: Note ID

    Returns:
        {"items": [...], "progress": float|null, "total": int, "done": int}
    """
    async with async_session_factory() as session:
        if await _fetch_note(session, note_id) is None:
            return {"error": f"Note {note_id} not found"}
        items = await list_items(session, note_id)
        progress = await compute_progress(session, note_id)
    done = sum(1 for i in items if i["status"] == "done")
    return {"items": items, "progress": progress, "total": len(items), "done": done}


@mcp.tool()
async def para_add_item(note_id: int, content: str) -> dict:
    """Add an action item (subtask) to a note.

    Args:
        note_id: Note ID
        content: What needs to be done

    Returns:
        The created item, or ``{"error": ...}`` if note not found.
    """
    if not content or not content.strip():
        return {"error": "content is required"}
    async with async_session_factory() as session:
        if await _fetch_note(session, note_id) is None:
            return {"error": f"Note {note_id} not found"}
        item = await create_item(session, note_id, content.strip())
        await sync_note_progress(session, note_id)
        await session.commit()
    return item


@mcp.tool()
async def para_done_item(item_id: int) -> dict:
    """Mark an action item as done.

    Args:
        item_id: Action item ID

    Returns:
        The updated item, or ``{"error": ...}`` if not found.
    """
    async with async_session_factory() as session:
        item = await update_item(session, item_id, status="done")
        if item is None:
            return {"error": f"Item {item_id} not found"}
        await sync_note_progress(session, item["note_id"])
        await session.commit()
    return item


@mcp.tool()
async def para_plan(horizon_days: int = 7) -> dict:
    """Generate a suggested plan for the next N days — prioritized actions,
    stale items to revisit, and a focus recommendation.

    Args:
        horizon_days: Planning horizon in days (default 7)

    Returns:
        Plan dict with prioritized_actions, stale_to_revisit, suggested_focus.
    """
    async with async_session_factory() as session:
        return await _pg_generate_plan(session, horizon_days)


@mcp.tool()
async def para_feedback_stats(days: int = 30) -> dict:
    """Classification feedback analytics — accuracy, common corrections, suggestions.

    Args:
        days: Look-back period (default 30)

    Returns:
        Feedback stats with accuracy_by_category, common_corrections, suggestions.
    """
    return await get_feedback_stats(days)


def main() -> None:
    """Initialize the database and run the MCP server over stdio."""
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print(__doc__)
        return

    para_db = os.environ.get("PARA_DB")
    if para_db:
        settings.PARA_DB_PATH = para_db
    asyncio.run(init_db())
    try:
        mcp.run(transport="stdio")
    except (KeyboardInterrupt, BrokenPipeError):
        logger.info("MCP client disconnected, shutting down")
    except Exception:
        logger.exception("MCP server crashed")
        raise


if __name__ == "__main__":
    main()
