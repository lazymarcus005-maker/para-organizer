"""Action items service: checklist items attached to notes (SB-06)."""

import logging
import re

import aiosqlite

logger = logging.getLogger("para.items")

ITEM_STATUSES = ("todo", "doing", "done")

_LIST_LINE_RE = re.compile(r"^\s*(?:\d+[.)]|[-•])\s+(.+?)\s*$")


def _row_to_item(row) -> dict:
    return dict(row)


async def _get_item(db: aiosqlite.Connection, item_id: int) -> dict | None:
    cursor = await db.execute("SELECT * FROM action_items WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_item(row)


async def create_item(
    db: aiosqlite.Connection,
    note_id: int,
    content: str,
    order_index: int | None = None,
) -> dict:
    if order_index is None:
        cursor = await db.execute(
            "SELECT MAX(order_index) AS m FROM action_items WHERE note_id = ?",
            (note_id,),
        )
        row = await cursor.fetchone()
        order_index = (row["m"] + 1) if row["m"] is not None else 0

    cursor = await db.execute(
        "INSERT INTO action_items (note_id, content, order_index) VALUES (?, ?, ?)",
        (note_id, content, order_index),
    )
    item_id = cursor.lastrowid
    await db.commit()
    logger.info("Created action item #%s for note #%s", item_id, note_id)
    return await _get_item(db, item_id)


async def list_items(db: aiosqlite.Connection, note_id: int) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM action_items WHERE note_id = ? ORDER BY order_index",
        (note_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_item(r) for r in rows]


async def update_item(
    db: aiosqlite.Connection,
    item_id: int,
    content: str | None = None,
    status: str | None = None,
) -> dict | None:
    existing = await _get_item(db, item_id)
    if existing is None:
        return None

    new_content = content if content is not None else existing["content"]
    new_status = status if status is not None else existing["status"]

    if status is not None and status != existing["status"]:
        if status == "done":
            completed_at: str | None = "CURRENT_TIMESTAMP"
        else:
            completed_at = None
    else:
        completed_at = existing["completed_at"]

    if completed_at == "CURRENT_TIMESTAMP":
        await db.execute(
            "UPDATE action_items SET content = ?, status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_content, new_status, item_id),
        )
    else:
        await db.execute(
            "UPDATE action_items SET content = ?, status = ?, completed_at = ? WHERE id = ?",
            (new_content, new_status, completed_at, item_id),
        )
    await db.commit()
    logger.info("Updated action item #%s (status=%s)", item_id, new_status)
    return await _get_item(db, item_id)


async def delete_item(db: aiosqlite.Connection, item_id: int) -> bool:
    cursor = await db.execute("DELETE FROM action_items WHERE id = ?", (item_id,))
    await db.commit()
    deleted = cursor.rowcount > 0
    if deleted:
        logger.info("Deleted action item #%s", item_id)
    return deleted


async def compute_progress(db: aiosqlite.Connection, note_id: int) -> float | None:
    cursor = await db.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done "
        "FROM action_items WHERE note_id = ?",
        (note_id,),
    )
    row = await cursor.fetchone()
    total = row["total"] or 0
    if total == 0:
        return None
    done = row["done"] or 0
    return round(done / total * 100, 2)


async def sync_note_progress(db: aiosqlite.Connection, note_id: int) -> None:
    progress = await compute_progress(db, note_id)
    await db.execute(
        "UPDATE notes SET progress = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (progress, note_id),
    )
    await db.commit()
    logger.info("Synced progress for note #%s: %s", note_id, progress)


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
