"""/api/notes/* — note CRUD, move, archive, re-classify."""

import hmac
import json
import logging

import aiosqlite
from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.classifier import classify_note, extract_deadline_from_text
from app.config import settings
from app.database import get_db
from app.events import emit_event
from app.feedback import record_feedback
from app.items import create_item, extract_items_from_content, sync_note_progress
from app.linker import auto_link_note
from app.models import NoteCreate, NoteMove, NoteUpdate, PARA_CATEGORIES, PRIORITIES, STATUSES
from app.tasks import suggest_task_from_note
from app.utils import row_to_note
from app.vector_store import delete_note_embedding, index_note

logger = logging.getLogger("para.routes.notes")
router = APIRouter(prefix="/api", tags=["notes"])

# Fields that must never be written as SQL NULL (NOT NULL columns).
_NON_NULLABLE_UPDATE_FIELDS = {"title", "content", "para_category", "status", "priority", "tags"}


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {settings.PARA_SECRET_KEY}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _log_history(db: aiosqlite.Connection, note_id: int, action: str,
                        old_value: str | None = None, new_value: str | None = None,
                        reason: str | None = None) -> None:
    await db.execute(
        "INSERT INTO history (note_id, action, old_value, new_value, reason) VALUES (?, ?, ?, ?, ?)",
        (note_id, action, old_value, new_value, reason),
    )


async def _fetch_note(db: aiosqlite.Connection, note_id: int) -> dict:
    cursor = await db.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return row_to_note(row)


@router.post("/notes", dependencies=[Depends(require_api_key)])
async def create_note(payload: NoteCreate, db: aiosqlite.Connection = Depends(get_db)):
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
        deadline = result.get("deadline")
        if not payload.tags_override:
            tags = result.get("tags", [])
        llm_model = result.get("llm_model")
        llm_confidence = float(result.get("confidence", 0.0))
        llm_reasoning = result.get("reasoning")

        if not deadline:
            extracted = extract_deadline_from_text(payload.content)
            deadline = extracted.isoformat() if extracted else None

    cursor = await db.execute(
        """
        INSERT INTO notes (title, content, para_category, sub_category, priority, deadline,
                            tags, source, llm_model, llm_confidence, llm_reasoning)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.title, payload.content, para_category, sub_category, priority, deadline,
            json.dumps(tags, ensure_ascii=False), payload.source, llm_model, llm_confidence, llm_reasoning,
        ),
    )
    note_id = cursor.lastrowid
    await _log_history(db, note_id, "created", new_value=payload.source)
    if payload.auto_classify:
        await _log_history(db, note_id, "classified", new_value=para_category, reason=llm_reasoning)
    await db.commit()

    try:
        await index_note(db, note_id, payload.content)
    except Exception:
        logger.warning("Failed to index embedding for note %s", note_id, exc_info=True)

    try:
        linked = await auto_link_note(db, note_id)
        if linked:
            logger.info("Auto-linked note %s to %s notes", note_id, linked)
    except Exception:
        logger.warning("Failed to auto-link note %s", note_id, exc_info=True)

    try:
        await emit_event(db, "note.created", note_id, {
            "title": payload.title,
            "para_category": para_category,
            "source": payload.source,
        })
    except Exception:
        logger.warning("Failed to emit note.created for note %s", note_id, exc_info=True)

    note = await _fetch_note(db, note_id)

    if settings.TASK_AUTO_EXTRACT:
        try:
            suggested = await suggest_task_from_note(payload.title, payload.content)
            if suggested:
                note["suggested_task"] = suggested
        except Exception:
            logger.warning("Failed to suggest task for note %s", note_id, exc_info=True)

    try:
        extracted = await extract_items_from_content(payload.content)
        if extracted:
            for text in extracted:
                await create_item(db, note_id, text)
            await sync_note_progress(db, note_id)
            note["items_created"] = len(extracted)
    except Exception:
        logger.warning("Failed to extract action items for note %s", note_id, exc_info=True)

    return note


@router.get("/notes")
async def list_notes(
    category: str | None = Query(default=None),
    status: str | None = Query(default=None),
    source: str | None = Query(default=None),
    review: bool | None = Query(default=None),
    limit: int = Query(default=20, le=200),
    offset: int = Query(default=0, ge=0),
    db: aiosqlite.Connection = Depends(get_db),
):
    clauses = []
    params: list = []
    if category:
        clauses.append("para_category = ?")
        params.append(category)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if source:
        clauses.append("source = ?")
        params.append(source)
    if review is not None:
        # A note "needs review" when it was LLM-classified below the confidence
        # threshold (see app.utils.row_to_note / classifier._apply_confidence_routing).
        predicate = "(llm_model IS NOT NULL AND llm_confidence < ?)"
        clauses.append(predicate if review else f"NOT {predicate}")
        params.append(settings.RECLASSIFY_CONFIDENCE_THRESHOLD)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    count_cursor = await db.execute(f"SELECT COUNT(*) AS c FROM notes {where}", params)
    total = (await count_cursor.fetchone())["c"]

    cursor = await db.execute(
        f"SELECT * FROM notes {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    rows = await cursor.fetchall()
    notes = [row_to_note(r) for r in rows]

    return {"notes": notes, "total": total, "limit": limit, "offset": offset}


@router.get("/notes/{note_id}")
async def get_note(note_id: int, db: aiosqlite.Connection = Depends(get_db)):
    return await _fetch_note(db, note_id)


@router.put("/notes/{note_id}")
async def update_note(note_id: int, payload: NoteUpdate, db: aiosqlite.Connection = Depends(get_db)):
    existing = await _fetch_note(db, note_id)

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return existing

    set_clauses = []
    params: list = []
    for key, value in fields.items():
        if key in _NON_NULLABLE_UPDATE_FIELDS and value is None and key != "tags":
            raise HTTPException(status_code=422, detail=f"{key} cannot be null")
        if key == "tags":
            value = json.dumps(value or [], ensure_ascii=False)
        elif key == "para_category" and value not in PARA_CATEGORIES:
            raise HTTPException(status_code=422, detail=f"Invalid para_category: {value}")
        elif key == "status" and value not in STATUSES:
            raise HTTPException(status_code=422, detail=f"Invalid status: {value}")
        elif key == "priority" and value not in PRIORITIES:
            raise HTTPException(status_code=422, detail=f"Invalid priority: {value}")
        set_clauses.append(f"{key} = ?")
        params.append(value)
    set_clauses.append("updated_at = CURRENT_TIMESTAMP")

    params.append(note_id)
    await db.execute(f"UPDATE notes SET {', '.join(set_clauses)} WHERE id = ?", params)
    await _log_history(db, note_id, "edited", old_value=json.dumps(existing, default=str, ensure_ascii=False),
                        new_value=json.dumps(fields, default=str, ensure_ascii=False))
    await db.commit()

    try:
        await index_note(db, note_id, fields.get("content") or existing["content"])
    except Exception:
        logger.warning("Failed to re-index embedding for note %s", note_id, exc_info=True)

    return await _fetch_note(db, note_id)


@router.delete("/notes/{note_id}")
async def delete_note(note_id: int, db: aiosqlite.Connection = Depends(get_db)):
    await _fetch_note(db, note_id)
    await db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    await db.commit()

    try:
        await delete_note_embedding(db, note_id)
    except Exception:
        pass

    return {"deleted": note_id}


@router.post("/notes/{note_id}/move")
async def move_note(note_id: int, payload: NoteMove, db: aiosqlite.Connection = Depends(get_db)):
    existing = await _fetch_note(db, note_id)
    if payload.para_category not in PARA_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"Invalid para_category: {payload.para_category}")

    await db.execute(
        "UPDATE notes SET para_category = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (payload.para_category, note_id),
    )
    await _log_history(db, note_id, "moved", old_value=existing["para_category"], new_value=payload.para_category)

    if existing["para_category"] != payload.para_category:
        try:
            await record_feedback(db, note_id, "para_category",
                                  existing["para_category"], payload.para_category)
        except Exception:
            logger.warning("Failed to record feedback for note %s", note_id, exc_info=True)

    await db.commit()

    return await _fetch_note(db, note_id)


@router.post("/notes/{note_id}/archive")
async def archive_note(note_id: int, db: aiosqlite.Connection = Depends(get_db)):
    existing = await _fetch_note(db, note_id)
    await db.execute(
        """UPDATE notes SET para_category = 'archives', status = 'archived',
           archived_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (note_id,),
    )
    await _log_history(db, note_id, "archived", old_value=existing["para_category"], new_value="archives")
    await db.commit()

    return await _fetch_note(db, note_id)


@router.post("/classify/{note_id}")
async def reclassify_note(note_id: int, db: aiosqlite.Connection = Depends(get_db)):
    existing = await _fetch_note(db, note_id)
    result = await classify_note(existing["title"], existing["content"])

    deadline = result.get("deadline")
    if not deadline:
        extracted = extract_deadline_from_text(existing["content"])
        deadline = extracted.isoformat() if extracted else None

    await db.execute(
        """
        UPDATE notes SET para_category = ?, sub_category = ?, priority = ?, deadline = ?,
                          tags = ?, llm_model = ?, llm_confidence = ?, llm_reasoning = ?,
                          updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            result.get("para_category", "inbox"), result.get("sub_category"), result.get("priority", "medium"),
            deadline, json.dumps(result.get("tags", []), ensure_ascii=False), result.get("llm_model"),
            float(result.get("confidence", 0.0)), result.get("reasoning"), note_id,
        ),
    )
    await _log_history(db, note_id, "classified", old_value=existing["para_category"],
                        new_value=result.get("para_category"), reason=result.get("reasoning"))
    await db.commit()

    try:
        await emit_event(db, "note.classified", note_id, {
            "para_category": result.get("para_category", "inbox"),
            "priority": result.get("priority", "medium"),
            "llm_confidence": float(result.get("confidence", 0.0)),
        })
    except Exception:
        logger.warning("Failed to emit note.classified for note %s", note_id, exc_info=True)

    return await _fetch_note(db, note_id)


@router.get("/graph")
async def get_graph_data(db: aiosqlite.Connection = Depends(get_db)):
    """Return nodes (notes) and edges (links) for graph visualization.
    
    Nodes include: id, label (title), size (priority weight), para_category.
    Edges include: from_note_id, to_note_id, link_type.
    """
    # Fetch all active notes to use as nodes
    cursor = await db.execute(
        "SELECT id, title, para_category, priority FROM notes WHERE status != 'archived'",
    )
    note_rows = await cursor.fetchall()
    
    # Build nodes with priority weighting
    priority_weights = {"low": 1, "medium": 2, "high": 3, "urgent": 4}
    nodes = []
    note_ids = set()
    for row in note_rows:
        note_id = row["id"]
        note_ids.add(note_id)
        nodes.append({
            "id": note_id,
            "label": row["title"],
            "category": row["para_category"],
            "size": priority_weights.get(row["priority"], 2) * 10,  # Scale for vis-network
        })
    
    # Fetch all links (edges) - only include if both nodes exist
    cursor = await db.execute(
        "SELECT id, from_note_id, to_note_id, link_type FROM links",
    )
    link_rows = await cursor.fetchall()
    
    edges = []
    seen_edges = set()
    for row in link_rows:
        from_id = row["from_note_id"]
        to_id = row["to_note_id"]
        # Only include edges where both notes exist and aren't archived
        if from_id in note_ids and to_id in note_ids:
            edge_key = (from_id, to_id)
            if edge_key not in seen_edges:
                edges.append({
                    "from": from_id,
                    "to": to_id,
                    "label": row["link_type"],
                    "link_type": row["link_type"],
                })
                seen_edges.add(edge_key)
    
    return {
        "nodes": nodes,
        "edges": edges,
    }
