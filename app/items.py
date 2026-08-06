"""Action items service: checklist items attached to notes (SB-06).

Backed by the `items` table (id, note_id, text, done, created_at) — simpler
than the three-state (todo/doing/done) + order_index schema this module
originally targeted under SQLite. `status` in the API is derived from `done`:
"done" or "todo" (there is no persisted "doing" state).
"""

import logging
import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database_v2 import async_session_factory
from app.models_v2 import Item

logger = logging.getLogger("para.items")

ITEM_STATUSES = ("todo", "doing", "done")

_LIST_LINE_RE = re.compile(r"^\s*(?:\d+[.)]|[-•])\s+(.+?)\s*$")


def _row_to_item(item: Item) -> dict:
    return {
        "id": item.id,
        "note_id": item.note_id,
        "content": item.text,
        "status": "done" if item.done else "todo",
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


async def create_item(session: AsyncSession | None, note_id: int, content: str) -> dict:
    item = Item(note_id=note_id, text=content, done=False)
    if session is not None:
        session.add(item)
        await session.flush()
        await session.refresh(item)
    else:
        async with async_session_factory() as owned:
            owned.add(item)
            await owned.commit()
            await owned.refresh(item)
    logger.info("Created action item #%s for note #%s", item.id, note_id)
    return _row_to_item(item)


async def list_items(session: AsyncSession, note_id: int) -> list[dict]:
    rows = (await session.execute(
        select(Item).where(Item.note_id == note_id).order_by(Item.id)
    )).scalars().all()
    return [_row_to_item(r) for r in rows]


async def update_item(
    session: AsyncSession,
    item_id: int,
    content: str | None = None,
    status: str | None = None,
) -> dict | None:
    item = await session.get(Item, item_id)
    if item is None:
        return None

    if content is not None:
        item.text = content
    if status is not None:
        item.done = status == "done"
    await session.flush()
    logger.info("Updated action item #%s (status=%s)", item_id, status)
    return _row_to_item(item)


async def delete_item(session: AsyncSession, item_id: int) -> bool:
    item = await session.get(Item, item_id)
    if item is None:
        return False
    await session.delete(item)
    await session.flush()
    logger.info("Deleted action item #%s", item_id)
    return True


async def compute_progress(session: AsyncSession, note_id: int) -> float | None:
    total = (await session.execute(
        select(func.count()).select_from(Item).where(Item.note_id == note_id)
    )).scalar_one()
    if total == 0:
        return None
    done = (await session.execute(
        select(func.count()).select_from(Item).where(Item.note_id == note_id, Item.done.is_(True))
    )).scalar_one()
    return round(done / total * 100, 2)


async def sync_note_progress(session: AsyncSession | None, note_id: int) -> None:
    """Compute the note's item-completion percentage.

    Not persisted: `notes` has no `progress` column in PostgreSQL (it wasn't
    read by any template — see app/templates/board.html), so this is
    read-only bookkeeping for callers that want the value.
    """
    if session is not None:
        progress = await compute_progress(session, note_id)
    else:
        async with async_session_factory() as owned:
            progress = await compute_progress(owned, note_id)
    logger.info("Computed progress for note #%s: %s", note_id, progress)


async def extract_items_from_content(content: str) -> list[str]:
    items: list[str] = []
    for line in content.splitlines():
        match = _LIST_LINE_RE.match(line)
        if not match:
            continue
        text = match.group(1).strip()
        if len(text) < 3 or text.startswith("#"):
            continue
        items.append(text)
    return items
