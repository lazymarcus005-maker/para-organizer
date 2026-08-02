"""/api/feedback/* — classification feedback loop (SB-08)."""

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from app.database import get_db
from app.feedback import get_feedback_stats, record_feedback
from app.models import FeedbackCreate
from app.routes.notes import require_api_key

logger = logging.getLogger("para.routes.feedback")
router = APIRouter(prefix="/api", tags=["feedback"])


@router.get("/feedback/stats")
async def feedback_stats(days: int = Query(default=30, ge=1, le=365)):
    return await get_feedback_stats(days)


@router.post("/feedback", dependencies=[Depends(require_api_key)])
async def create_feedback(payload: FeedbackCreate, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT * FROM notes WHERE id = ?", (payload.note_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Note not found")

    llm_value = dict(row).get(payload.field)
    if llm_value is not None:
        llm_value = str(llm_value)

    feedback = await record_feedback(db, payload.note_id, payload.field, llm_value, payload.user_value)
    await db.commit()
    return feedback
