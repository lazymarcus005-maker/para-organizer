"""/api/notes/{id}/items & /api/items/* — action items & subtasks (SB-06)."""

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from app.database import get_db
from app.items import (
    ITEM_STATUSES,
    create_item,
    delete_item,
    extract_items_from_content,
    list_items,
    sync_note_progress,
    update_item,
)
from app.models import ItemCreate, ItemUpdate
from app.routes.notes import require_api_key

logger = logging.getLogger("para.routes.items")
router = APIRouter(prefix="/api", tags=["items"])


@router.get("/notes/{note_id}/items")
async def list_items_endpoint(note_id: int, db: aiosqlite.Connection = Depends(get_db)):
    items = await list_items(db, note_id)
    return {"items": items}


@router.post("/notes/{note_id}/items", dependencies=[Depends(require_api_key)])
async def create_item_endpoint(note_id: int, payload: ItemCreate, db: aiosqlite.Connection = Depends(get_db)):
    item = await create_item(db, note_id, payload.content)
    await sync_note_progress(db, note_id)
    return item


@router.put("/items/{item_id}", dependencies=[Depends(require_api_key)])
async def update_item_endpoint(item_id: int, payload: ItemUpdate, db: aiosqlite.Connection = Depends(get_db)):
    if payload.status is not None and payload.status not in ITEM_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status: {payload.status}")
    item = await update_item(db, item_id, content=payload.content, status=payload.status)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    await sync_note_progress(db, item["note_id"])
    return item


@router.delete("/items/{item_id}", dependencies=[Depends(require_api_key)])
async def delete_item_endpoint(item_id: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT note_id FROM action_items WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")
    note_id = row["note_id"]
    await delete_item(db, item_id)
    await sync_note_progress(db, note_id)
    return {"deleted": item_id}


@router.post("/notes/{note_id}/items/extract", dependencies=[Depends(require_api_key)])
async def extract_items_endpoint(note_id: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT content FROM notes WHERE id = ?", (note_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Note not found")

    extracted = await extract_items_from_content(row["content"])
    created = [await create_item(db, note_id, text) for text in extracted]
    if created:
        await sync_note_progress(db, note_id)
    return {"items": created, "items_created": len(created)}
