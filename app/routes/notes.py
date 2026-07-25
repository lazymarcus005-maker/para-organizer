"""/api/notes/* — note CRUD, move, archive, re-classify."""

import json
import logging

import aiosqlite
from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.classifier import classify_note, extract_deadline_from_text
from app.config import settings
from app.database import get_db
from app.models import NoteCreate, NoteMove, NoteUpdate, PARA_CATEGORIES
from app.utils import row_to_note

logger = logging.getLogger("para.routes.notes")
router = APIRouter(prefix="/api", tags=["notes"])


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {settings.PARA_SECRET_KEY}"
    if authorization != expected:
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

    return await _fetch_note(db, note_id)


@router.get("/notes")
async def list_notes(
    category: str | None = Query(default=None),
    status: str | None = Query(default=None),
    source: str | None = Query(default=None),
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
        if key == "tags":
            value = json.dumps(value, ensure_ascii=False)
        if key == "para_category" and value not in PARA_CATEGORIES:
            raise HTTPException(status_code=422, detail=f"Invalid para_category: {value}")
        set_clauses.append(f"{key} = ?")
        params.append(value)
    set_clauses.append("updated_at = CURRENT_TIMESTAMP")

    params.append(note_id)
    await db.execute(f"UPDATE notes SET {', '.join(set_clauses)} WHERE id = ?", params)
    await _log_history(db, note_id, "edited", old_value=json.dumps(existing, default=str, ensure_ascii=False),
                        new_value=json.dumps(fields, default=str, ensure_ascii=False))
    await db.commit()

    return await _fetch_note(db, note_id)


@router.delete("/notes/{note_id}")
async def delete_note(note_id: int, db: aiosqlite.Connection = Depends(get_db)):
    await _fetch_note(db, note_id)
    await db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    await db.commit()
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

    return await _fetch_note(db, note_id)
