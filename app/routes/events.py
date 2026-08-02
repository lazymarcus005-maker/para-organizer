"""/api/events/* — list, inspect and retry outbound events."""

import json
import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from app.database import get_db
from app.events import dispatch_event
from app.routes.notes import require_api_key

router = APIRouter(prefix="/api", tags=["events"])
logger = logging.getLogger("para.routes.events")


def _row_to_event(row: aiosqlite.Row) -> dict:
    data = dict(row)
    try:
        data["payload"] = json.loads(data.get("payload") or "{}")
    except (ValueError, TypeError):
        pass
    return data


@router.get("/events")
async def list_events(
    event_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: aiosqlite.Connection = Depends(get_db),
):
    clauses = []
    params: list = []
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    count_cursor = await db.execute(f"SELECT COUNT(*) AS c FROM events {where}", params)
    total = (await count_cursor.fetchone())["c"]

    cursor = await db.execute(
        f"SELECT * FROM events {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    rows = await cursor.fetchall()
    events = [_row_to_event(r) for r in rows]

    return {"events": events, "total": total, "limit": limit, "offset": offset}


@router.get("/events/{event_id}")
async def get_event(event_id: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT * FROM events WHERE id = ?", (event_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return _row_to_event(row)


@router.post("/events/{event_id}/retry", dependencies=[Depends(require_api_key)])
async def retry_event(event_id: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT * FROM events WHERE id = ?", (event_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Event not found")

    event = _row_to_event(row)
    payload = event["payload"] if isinstance(event["payload"], dict) else {}
    success = await dispatch_event(event_id, event["event_type"], payload)

    cursor = await db.execute("SELECT * FROM events WHERE id = ?", (event_id,))
    updated = await cursor.fetchone()
    return _row_to_event(updated) if updated is not None else {**event, "status": "failed"}
