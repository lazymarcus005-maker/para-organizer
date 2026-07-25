"""Authenticated webhook for notes produced by Hermes cron jobs."""

import hmac
import json

import aiosqlite
from fastapi import APIRouter, Depends, Header, HTTPException

from app.classifier import classify_note, extract_deadline_from_text
from app.config import settings
from app.database import get_db
from app.models import CronNoteCreate
from app.utils import row_to_note

router = APIRouter(prefix="/api", tags=["cron"])


@router.post("/notes/cron")
async def create_note_from_cron(
    payload: CronNoteCreate,
    authorization: str | None = Header(default=None),
    db: aiosqlite.Connection = Depends(get_db),
):
    expected = f"Bearer {settings.PARA_SECRET_KEY}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not payload.source.startswith("cron:"):
        raise HTTPException(status_code=422, detail="source must be cron:<job_name>")

    title = payload.title or payload.source
    result = await classify_note(title, payload.content) if payload.auto_classify else {}
    deadline = result.get("deadline")
    if payload.auto_classify and not deadline:
        extracted = extract_deadline_from_text(payload.content)
        deadline = extracted.isoformat() if extracted else None
    tags = payload.tags_override if payload.tags_override is not None else result.get("tags", [])

    cursor = await db.execute(
        """INSERT INTO notes
           (title, content, para_category, sub_category, priority, deadline, tags,
            source, llm_model, llm_confidence, llm_reasoning)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            title, payload.content, result.get("para_category", "inbox"),
            result.get("sub_category"), result.get("priority", "medium"), deadline,
            json.dumps(tags, ensure_ascii=False), payload.source, result.get("llm_model"),
            float(result.get("confidence", 0.0)), result.get("reasoning"),
        ),
    )
    note_id = cursor.lastrowid
    await db.execute(
        "INSERT INTO history (note_id, action, new_value) VALUES (?, 'created', ?)",
        (note_id, payload.source),
    )
    await db.commit()
    row = await (await db.execute("SELECT * FROM notes WHERE id = ?", (note_id,))).fetchone()
    return row_to_note(row)

