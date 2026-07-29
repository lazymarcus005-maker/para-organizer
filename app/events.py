"""Outbound event bus: persist events and dispatch them to a webhook (SB-01)."""

import asyncio
import json
import logging
from datetime import datetime, timezone

import aiosqlite
import httpx

from app.config import settings
from app.database import get_connection

logger = logging.getLogger("para.events")


def enabled_event_types() -> set[str]:
    return {item.strip() for item in settings.EVENT_TYPES_ENABLED.split(",") if item.strip()}


async def _set_status(event_id: int, status: str) -> None:
    async with get_connection() as db:
        if status == "delivered":
            await db.execute(
                "UPDATE events SET status = ?, delivered_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, event_id),
            )
        else:
            await db.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))
        await db.commit()


async def dispatch_event(event_id: int, event_type: str, payload: dict) -> bool:
    url = settings.EVENT_WEBHOOK_URL
    if not url:
        await _set_status(event_id, "failed")
        return False

    note_id = None
    timestamp = datetime.now(timezone.utc).isoformat()
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT note_id, created_at FROM events WHERE id = ?", (event_id,)
        )).fetchone()
        if row is not None:
            note_id = row["note_id"]
            timestamp = row["created_at"] or timestamp

    body = {
        "event_type": event_type,
        "note_id": note_id,
        "payload": payload,
        "timestamp": timestamp,
    }
    headers = {}
    if settings.EVENT_WEBHOOK_SECRET:
        headers["X-Webhook-Secret"] = settings.EVENT_WEBHOOK_SECRET

    retries = max(1, settings.EVENT_DISPATCH_RETRIES)
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
            await _set_status(event_id, "delivered")
            logger.info("Dispatched event %s (%s)", event_id, event_type)
            return True
        except Exception:
            logger.warning(
                "Event %s dispatch attempt %d/%d failed", event_id, attempt + 1, retries,
                exc_info=True,
            )
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)

    await _set_status(event_id, "failed")
    return False


async def emit_event(db: aiosqlite.Connection, event_type: str, note_id: int | None = None,
                     payload: dict | None = None) -> int | None:
    if event_type not in enabled_event_types():
        return None

    payload = payload or {}
    cursor = await db.execute(
        "INSERT INTO events (event_type, note_id, payload, status) VALUES (?, ?, ?, 'pending')",
        (event_type, note_id, json.dumps(payload, ensure_ascii=False)),
    )
    event_id = cursor.lastrowid
    await db.commit()

    if settings.EVENT_WEBHOOK_URL:
        try:
            await dispatch_event(event_id, event_type, payload)
        except Exception:
            logger.exception("Failed to dispatch event %s", event_id)

    return event_id
