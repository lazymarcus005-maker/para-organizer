"""Outbound event bus: persist events and dispatch them to a webhook (SB-01)."""

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database_v2 import async_session_factory
from app.models_v2 import Event

logger = logging.getLogger("para.events")


def enabled_event_types() -> set[str]:
    return {item.strip() for item in settings.EVENT_TYPES_ENABLED.split(",") if item.strip()}


async def _set_status(event_id: int, status: str) -> None:
    async with async_session_factory() as session:
        event = await session.get(Event, event_id)
        if event is not None:
            event.status = status
            if status == "delivered":
                event.delivered_at = datetime.now(timezone.utc)
        await session.commit()


async def dispatch_event(event_id: int, event_type: str, payload: dict) -> bool:
    url = settings.EVENT_WEBHOOK_URL
    if not url:
        await _set_status(event_id, "failed")
        return False

    note_id = None
    timestamp = datetime.now(timezone.utc).isoformat()
    async with async_session_factory() as session:
        event = await session.get(Event, event_id)
        if event is not None:
            note_id = event.note_id
            timestamp = event.created_at.isoformat() if event.created_at else timestamp

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


async def emit_event(session: AsyncSession | None, event_type: str, note_id: int | None = None,
                     payload: dict | None = None) -> int | None:
    """Persist an event. Uses `session` if given (participates in the caller's
    transaction, not yet committed), otherwise opens and commits its own."""
    if event_type not in enabled_event_types():
        return None

    payload = payload or {}
    event = Event(event_type=event_type, note_id=note_id, payload=payload, status="pending")

    if session is not None:
        session.add(event)
        await session.commit()
        event_id = event.id
    else:
        async with async_session_factory() as owned:
            owned.add(event)
            await owned.commit()
            event_id = event.id

    if settings.EVENT_WEBHOOK_URL:
        try:
            await dispatch_event(event_id, event_type, payload)
        except Exception:
            logger.exception("Failed to dispatch event %s", event_id)

    return event_id
