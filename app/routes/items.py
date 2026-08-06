"""/api/notes/{id}/items & /api/items/* — action items & subtasks (SB-06)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database_v2 import get_db as get_pg_db
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
from app.models_v2 import Item, Note
from app.routes_v2 import require_api_key

logger = logging.getLogger("para.routes.items")
router = APIRouter(prefix="/api", tags=["items"])


@router.get("/notes/{note_id}/items")
async def list_items_endpoint(note_id: int, session: AsyncSession = Depends(get_pg_db)):
    items = await list_items(session, note_id)
    return {"items": items}


@router.post("/notes/{note_id}/items", dependencies=[Depends(require_api_key)])
async def create_item_endpoint(note_id: int, payload: ItemCreate, session: AsyncSession = Depends(get_pg_db)):
    item = await create_item(session, note_id, payload.content)
    await sync_note_progress(session, note_id)
    return item


@router.put("/items/{item_id}", dependencies=[Depends(require_api_key)])
async def update_item_endpoint(item_id: int, payload: ItemUpdate, session: AsyncSession = Depends(get_pg_db)):
    if payload.status is not None and payload.status not in ITEM_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status: {payload.status}")
    item = await update_item(session, item_id, content=payload.content, status=payload.status)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    await sync_note_progress(session, item["note_id"])
    return item


@router.delete("/items/{item_id}", dependencies=[Depends(require_api_key)])
async def delete_item_endpoint(item_id: int, session: AsyncSession = Depends(get_pg_db)):
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    note_id = item.note_id
    await delete_item(session, item_id)
    await sync_note_progress(session, note_id)
    return {"deleted": item_id}


@router.post("/notes/{note_id}/items/extract", dependencies=[Depends(require_api_key)])
async def extract_items_endpoint(note_id: int, session: AsyncSession = Depends(get_pg_db)):
    note = await session.get(Note, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")

    extracted = await extract_items_from_content(note.content)
    created = [await create_item(session, note_id, text) for text in extracted]
    if created:
        await sync_note_progress(session, note_id)
    return {"items": created, "items_created": len(created)}
