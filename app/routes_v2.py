"""Routes for PARA Organizer — PostgreSQL version.

/api/notes/* — note CRUD, move, archive, re-classify.
/api/search — full-text search via PostgreSQL tsvector.
/api/settings — read and update runtime settings.
/api/graph — nodes and edges for graph visualization.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select, text, delete as sa_delete, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.classifier import classify_note, extract_deadline_from_text
from app.config import _cast_bool, settings
from app.database_v2 import get_db as get_pg_db
from app.events import emit_event
from app.feedback import record_feedback
from app.items import create_item, extract_items_from_content, sync_note_progress
from app.linker import auto_link_note
from app.models import NoteCreate, NoteMove, NoteUpdate, PARA_CATEGORIES, PRIORITIES, STATUSES
from app.models_v2 import Note, Link, History, Setting, ChatMessage, LlmUsage
from app.scheduler import digest_trigger, reclassify_trigger, scheduler
from app.settings_helper import get_env_settings_groups
from app.tasks import suggest_task_from_note
from app.utils import row_to_note
from app.vector_store import delete_note_embedding, index_note

logger = logging.getLogger("para.routes")

router = APIRouter(prefix="/api", tags=["notes"])
search_router = APIRouter(prefix="/api/search", tags=["search"])
context_router = APIRouter(prefix="/api", tags=["context"])
settings_router = APIRouter(prefix="/api", tags=["settings"])

_NON_NULLABLE_UPDATE_FIELDS = {"title", "content", "para_category", "status", "priority", "tags"}

# ── Auth ──────────────────────────────────────────────────────────────────────

def require_api_key(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {settings.PARA_SECRET_KEY}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")

# ── Helpers ───────────────────────────────────────────────────────────────────

async def _log_history(session: AsyncSession, note_id: int, action: str,
                        old_value: str | None = None, new_value: str | None = None,
                        reason: str | None = None) -> None:
    session.add(History(
        note_id=note_id, action=action,
        old_value=old_value, new_value=new_value, reason=reason,
    ))

async def _fetch_note(session: AsyncSession, note_id: int) -> dict:
    result = await session.execute(select(Note).where(Note.id == note_id))
    note = result.scalar_one_or_none()
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
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
        "tags": note.tags if isinstance(note.tags, list) else json.loads(note.tags) if isinstance(note.tags, str) else [],
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
    }

# ── Notes CRUD ────────────────────────────────────────────────────────────────

@router.post("/notes", dependencies=[Depends(require_api_key)])
async def create_note(payload: NoteCreate, session: AsyncSession = Depends(get_pg_db)):
    para_category = "inbox"
    sub_category = None
    priority = "medium"
    deadline = None
    tags = payload.tags_override or []
    llm_model = None
    llm_confidence = 0.0
    llm_reasoning = None

    if payload.auto_classify:
        result = await classify_note(payload.title, payload.content)
        para_category = result.get("para_category", "inbox")
        sub_category = result.get("sub_category")
        priority = result.get("priority", "medium")
        dl = result.get("deadline")
        deadline = date.fromisoformat(dl) if dl else None
        if not payload.tags_override:
            tags = result.get("tags", [])
        llm_model = result.get("llm_model")
        llm_confidence = float(result.get("confidence", 0.0))
        llm_reasoning = result.get("reasoning")
        if not deadline:
            extracted = extract_deadline_from_text(payload.content)
            deadline = extracted if extracted else None

    note = Note(
        title=payload.title, content=payload.content,
        para_category=para_category, sub_category=sub_category,
        priority=priority, deadline=deadline,
        tags=tags, source=payload.source,
        llm_model=llm_model, llm_confidence=llm_confidence, llm_reasoning=llm_reasoning,
    )
    session.add(note)
    await session.flush()
    note_id = note.id

    await _log_history(session, note_id, "created", new_value=payload.source)
    if payload.auto_classify:
        await _log_history(session, note_id, "classified", new_value=para_category, reason=llm_reasoning)

    # Enqueue background tasks
    try:
        from app.task_queue import TaskQueue
        queue = TaskQueue()
        await queue.publish("classify", {"note_id": note_id, "source": payload.source})
        await queue.publish("embed", {"note_id": note_id})
        await queue.publish("link", {"note_id": note_id})
        await queue.close()
    except Exception:
        logger.warning("Failed to enqueue background tasks for note %s", note_id, exc_info=True)

    try:
        await index_note(None, note_id, payload.content)
    except Exception:
        logger.warning("Failed to index embedding for note %s", note_id, exc_info=True)

    try:
        linked = await auto_link_note(None, note_id)
        if linked:
            logger.info("Auto-linked note %s to %s notes", note_id, linked)
    except Exception:
        logger.warning("Failed to auto-link note %s", note_id, exc_info=True)

    try:
        await emit_event(None, "note.created", note_id, {
            "title": payload.title, "para_category": para_category, "source": payload.source,
        })
    except Exception:
        logger.warning("Failed to emit note.created for note %s", note_id, exc_info=True)

    result = await _fetch_note(session, note_id)

    if settings.TASK_AUTO_EXTRACT:
        try:
            suggested = await suggest_task_from_note(payload.title, payload.content)
            if suggested:
                result["suggested_task"] = suggested
        except Exception:
            logger.warning("Failed to suggest task for note %s", note_id, exc_info=True)

    try:
        extracted = await extract_items_from_content(payload.content)
        if extracted:
            for text in extracted:
                await create_item(None, note_id, text)
            await sync_note_progress(None, note_id)
            result["items_created"] = len(extracted)
    except Exception:
        logger.warning("Failed to extract action items for note %s", note_id, exc_info=True)

    return result


@router.get("/notes")
async def list_notes(
    category: str | None = Query(default=None),
    status: str | None = Query(default=None),
    source: str | None = Query(default=None),
    review: bool | None = Query(default=None),
    limit: int = Query(default=20, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_pg_db),
):
    stmt = select(Note)
    if category:
        stmt = stmt.where(Note.para_category == category)
    if status:
        stmt = stmt.where(Note.status == status)
    if source:
        stmt = stmt.where(Note.source == source)
    if review is not None:
        predicate = (Note.llm_model.isnot(None)) & (Note.llm_confidence < settings.RECLASSIFY_CONFIDENCE_THRESHOLD)
        stmt = stmt.where(predicate if review else ~predicate)

    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()

    stmt = stmt.order_by(Note.created_at.desc()).limit(limit).offset(offset)
    rows = (await session.execute(stmt)).scalars().all()

    notes = []
    for n in rows:
        notes.append({
            "id": n.id, "title": n.title, "content": n.content,
            "para_category": n.para_category, "sub_category": n.sub_category,
            "status": n.status, "priority": n.priority,
            "deadline": n.deadline.isoformat() if n.deadline else None,
            "tags": n.tags if isinstance(n.tags, list) else json.loads(n.tags) if isinstance(n.tags, str) else [],
            "source": n.source, "llm_model": n.llm_model,
            "llm_confidence": n.llm_confidence, "llm_reasoning": n.llm_reasoning,
            "embedding_status": n.embedding_status,
            "created_at": n.created_at.isoformat() if n.created_at else None,
            "updated_at": n.updated_at.isoformat() if n.updated_at else None,
        })

    return {"notes": notes, "total": total, "limit": limit, "offset": offset}


@router.get("/notes/{note_id}")
async def get_note(note_id: int, session: AsyncSession = Depends(get_pg_db)):
    return await _fetch_note(session, note_id)


@router.put("/notes/{note_id}")
async def update_note(note_id: int, payload: NoteUpdate, session: AsyncSession = Depends(get_pg_db)):
    existing = await _fetch_note(session, note_id)

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return existing

    update_data = {}
    for key, value in fields.items():
        if key in _NON_NULLABLE_UPDATE_FIELDS and value is None and key != "tags":
            raise HTTPException(status_code=422, detail=f"{key} cannot be null")
        if key == "tags":
            update_data[key] = json.dumps(value or [], ensure_ascii=False)
        elif key == "para_category" and value not in PARA_CATEGORIES:
            raise HTTPException(status_code=422, detail=f"Invalid para_category: {value}")
        elif key == "status" and value not in STATUSES:
            raise HTTPException(status_code=422, detail=f"Invalid status: {value}")
        elif key == "priority" and value not in PRIORITIES:
            raise HTTPException(status_code=422, detail=f"Invalid priority: {value}")
        else:
            update_data[key] = value

    update_data["updated_at"] = datetime.utcnow()

    await session.execute(
        sa_update(Note).where(Note.id == note_id).values(**update_data)
    )
    await _log_history(session, note_id, "edited",
                        old_value=json.dumps(existing, default=str, ensure_ascii=False),
                        new_value=json.dumps(fields, default=str, ensure_ascii=False))

    try:
        await index_note(None, note_id, fields.get("content") or existing["content"])
    except Exception:
        logger.warning("Failed to re-index embedding for note %s", note_id, exc_info=True)

    return await _fetch_note(session, note_id)


@router.delete("/notes/{note_id}")
async def delete_note(note_id: int, session: AsyncSession = Depends(get_pg_db)):
    await _fetch_note(session, note_id)
    await session.execute(sa_delete(Note).where(Note.id == note_id))

    try:
        await delete_note_embedding(None, note_id)
    except Exception:
        pass

    return {"deleted": note_id}


@router.post("/notes/{note_id}/move")
async def move_note(note_id: int, payload: NoteMove, session: AsyncSession = Depends(get_pg_db)):
    existing = await _fetch_note(session, note_id)
    if payload.para_category not in PARA_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"Invalid para_category: {payload.para_category}")

    await session.execute(
        sa_update(Note).where(Note.id == note_id).values(
            para_category=payload.para_category, updated_at=datetime.utcnow()
        )
    )
    await _log_history(session, note_id, "moved", old_value=existing["para_category"], new_value=payload.para_category)

    if existing["para_category"] != payload.para_category:
        try:
            await record_feedback(None, note_id, "para_category",
                                  existing["para_category"], payload.para_category)
        except Exception:
            logger.warning("Failed to record feedback for note %s", note_id, exc_info=True)

    return await _fetch_note(session, note_id)


@router.post("/notes/{note_id}/archive")
async def archive_note(note_id: int, session: AsyncSession = Depends(get_pg_db)):
    existing = await _fetch_note(session, note_id)
    await session.execute(
        sa_update(Note).where(Note.id == note_id).values(
            para_category="archives", status="archived",
            archived_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        )
    )
    await _log_history(session, note_id, "archived", old_value=existing["para_category"], new_value="archives")
    return await _fetch_note(session, note_id)


@router.post("/classify/{note_id}")
async def reclassify_note(note_id: int, session: AsyncSession = Depends(get_pg_db)):
    existing = await _fetch_note(session, note_id)
    result = await classify_note(existing["title"], existing["content"])

    dl = result.get("deadline")
    deadline = date.fromisoformat(dl) if dl else None
    if not deadline:
        extracted = extract_deadline_from_text(existing["content"])
        deadline = extracted if extracted else None

    await session.execute(
        sa_update(Note).where(Note.id == note_id).values(
            para_category=result.get("para_category", "inbox"),
            sub_category=result.get("sub_category"),
            priority=result.get("priority", "medium"),
            deadline=deadline,
            tags=json.dumps(result.get("tags", []), ensure_ascii=False),
            llm_model=result.get("llm_model"),
            llm_confidence=float(result.get("confidence", 0.0)),
            llm_reasoning=result.get("reasoning"),
            updated_at=datetime.utcnow(),
        )
    )
    await _log_history(session, note_id, "classified",
                        old_value=existing["para_category"],
                        new_value=result.get("para_category"),
                        reason=result.get("reasoning"))

    try:
        await emit_event(None, "note.classified", note_id, {
            "para_category": result.get("para_category", "inbox"),
            "priority": result.get("priority", "medium"),
            "llm_confidence": float(result.get("confidence", 0.0)),
        })
    except Exception:
        logger.warning("Failed to emit note.classified for note %s", note_id, exc_info=True)

    return await _fetch_note(session, note_id)


# ── Graph ──────────────────────────────────────────────────────────────────────

@router.get("/graph")
async def get_graph_data(session: AsyncSession = Depends(get_pg_db)):
    result = await session.execute(
        select(Note.id, Note.title, Note.para_category, Note.priority).where(Note.status != "archived")
    )
    note_rows = result.all()

    priority_weights = {"low": 1, "medium": 2, "high": 3, "urgent": 4}
    nodes = []
    note_ids = set()
    for row in note_rows:
        note_ids.add(row.id)
        nodes.append({
            "id": row.id,
            "label": row.title,
            "category": row.para_category,
            "size": priority_weights.get(row.priority, 2) * 10,
        })

    result = await session.execute(select(Link.id, Link.from_note_id, Link.to_note_id, Link.link_type))
    link_rows = result.all()

    edges = []
    seen_edges = set()
    for row in link_rows:
        if row.from_note_id in note_ids and row.to_note_id in note_ids:
            edge_key = (row.from_note_id, row.to_note_id)
            if edge_key not in seen_edges:
                edges.append({
                    "from": row.from_note_id,
                    "to": row.to_note_id,
                    "label": row.link_type,
                    "link_type": row.link_type,
                })
                seen_edges.add(edge_key)

    return {"nodes": nodes, "edges": edges}


# ── Search ─────────────────────────────────────────────────────────────────────

_SNIPPET_START = "\x01"
_SNIPPET_END = "\x02"


def _build_match_query(q: str) -> str:
    tokens = [t.replace('"', '') for t in q.split() if t.strip()]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"*' for t in tokens)


@search_router.get("")
async def search_notes(
    q: str = Query(default=""),
    limit: int = Query(default=20, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_pg_db),
):
    if not q.strip():
        return {"results": [], "total": 0}

    match_expr = _build_match_query(q)
    if not match_expr:
        return {"results": [], "total": 0}

    # PostgreSQL tsvector search
    query_ts = func.plainto_tsquery("simple", q)
    stmt = (
        select(Note)
        .where(Note.search_vector.op("@@")(query_ts))
        .order_by(func.ts_rank(Note.search_vector, query_ts).desc())
        .limit(limit)
        .offset(offset)
    )
    total_stmt = select(func.count()).select_from(
        select(Note).where(Note.search_vector.op("@@")(query_ts)).subquery()
    )

    total = (await session.execute(total_stmt)).scalar_one()
    rows = (await session.execute(stmt)).scalars().all()

    results = []
    for n in rows:
        results.append({
            "id": n.id, "title": n.title, "content": n.content,
            "para_category": n.para_category, "status": n.status,
            "priority": n.priority,
            "deadline": n.deadline.isoformat() if n.deadline else None,
            "tags": n.tags if isinstance(n.tags, list) else json.loads(n.tags) if isinstance(n.tags, str) else [],
            "created_at": n.created_at.isoformat() if n.created_at else None,
            "updated_at": n.updated_at.isoformat() if n.updated_at else None,
        })

    return {"results": results, "total": total}


@context_router.get("/context")
async def get_context(
    q: str = Query(default=""),
    limit: int = Query(default=5, le=20),
    session: AsyncSession = Depends(get_pg_db),
):
    from app.context import build_context
    return await build_context(None, q, limit)


# ── Settings ───────────────────────────────────────────────────────────────────

SETTINGS_KEYS: dict[str, type] = {
    "NOTIFY_DEADLINE_DAYS": str, "NOTIFY_DIGEST_DAY": str, "NOTIFY_DIGEST_TIME": str,
    "NOTIFY_STALE_DAYS": int, "NOTIFY_CHANNEL": str, "AUTO_ARCHIVE_DAYS": int,
    "RECLASSIFY_INTERVAL_HOURS": int, "RECLASSIFY_CONFIDENCE_THRESHOLD": float,
    "LLM_PRIMARY": str, "LLM_FALLBACK": str, "LLM_TIMEOUT": int, "LLM_MAX_RETRIES": int,
    "CHAT_MODEL": str, "CHAT_HISTORY_MAX": int, "CHAT_SYSTEM_PROMPT": str,
    "EMBED_PROVIDER": str, "EMBED_BASE_URL": str, "EMBED_MODEL": str,
    "RAG_HYBRID_ENABLED": _cast_bool, "RAG_HYBRID_RATIO": float, "RAG_SEARCH_LIMIT": int,
}


async def get_settings_dict(session: AsyncSession) -> dict:
    """Flat {KEY: cast_value} view of settings — DB override falling back to
    the env-configured default. Used for server-rendered pages."""
    result = await session.execute(select(Setting))
    db_settings = {row.key: row.value for row in result.scalars().all()}
    out: dict = {}
    for key, cast in SETTINGS_KEYS.items():
        if key in db_settings:
            try:
                out[key] = cast(db_settings[key])
                continue
            except (ValueError, TypeError):
                pass
        out[key] = getattr(settings, key)
    return out


@settings_router.get("/settings")
async def get_settings(session: AsyncSession = Depends(get_pg_db)):
    result = await session.execute(select(Setting))
    db_settings = {row.key: row.value for row in result.scalars().all()}
    env_groups = get_env_settings_groups(settings)

    merged = []
    for group_name, items in env_groups.items():
        for item in items:
            key = item["key"]
            if key in db_settings:
                item["value"] = db_settings[key]
                item["source"] = "database"
            merged.append(item)

    return {"settings": merged, "env_groups": env_groups}


@settings_router.put("/settings", dependencies=[Depends(require_api_key)])
async def update_settings(payload: dict[str, str], session: AsyncSession = Depends(get_pg_db)):
    errors = []
    for key, value in payload.items():
        if key not in SETTINGS_KEYS:
            errors.append({"key": key, "error": "Unknown setting key"})
            continue
        cast = SETTINGS_KEYS[key]
        try:
            cast(value)
        except (ValueError, TypeError):
            errors.append({"key": key, "error": f"Invalid value for type {cast.__name__}"})
            continue

        # Upsert
        existing = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
        if existing:
            existing.value = value
        else:
            session.add(Setting(key=key, value=value))

    if errors:
        raise HTTPException(status_code=422, detail=errors)

    # Re-apply runtime overrides
    for key, value in payload.items():
        cast = SETTINGS_KEYS[key]
        try:
            setattr(settings, key, cast(value))
        except (ValueError, TypeError):
            pass

    return {"updated": list(payload.keys()), "errors": errors}
