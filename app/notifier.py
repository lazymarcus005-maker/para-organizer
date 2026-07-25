"""Telegram notification delivery and message formatting."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

from telegram import Bot

from app.config import settings

logger = logging.getLogger("para.notifier")


def notification_chat_ids() -> list[int]:
    return [
        int(value.strip())
        for value in settings.TELEGRAM_ALLOWED_USERS.split(",")
        if value.strip().lstrip("-").isdigit()
    ]


async def send_telegram(
    chat_id: int, text: str, reply_markup: Any | None = None, retries: int = 3
) -> bool:
    """Send a Telegram message, retrying transient failures with backoff."""
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("Telegram token is not configured; message not sent")
        return False
    for attempt in range(1, retries + 1):
        try:
            logger.info("Sending Telegram message to %s (attempt %s)", chat_id, attempt)
            async with Bot(settings.TELEGRAM_BOT_TOKEN) as bot:
                await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
            return True
        except Exception:
            logger.exception("Telegram send to %s failed (attempt %s)", chat_id, attempt)
            if attempt < retries:
                await asyncio.sleep(2 ** (attempt - 1))
    return False


def _note_chat_ids(note: dict) -> list[int]:
    metadata = note.get("source_metadata") or {}
    chat_id = metadata.get("chat_id") if isinstance(metadata, dict) else None
    return [int(chat_id)] if chat_id is not None else notification_chat_ids()


async def notify_deadline(note: dict, days_left: int) -> bool:
    deadline = str(note.get("deadline", ""))
    text = (
        "⏰ ใกล้ถึงกำหนด!\n\n"
        f"📋 {note['title']}\n"
        f"📅 Deadline: {deadline}\n"
        f"⏰ เหลือ: {days_left} วัน\n"
        f"🔴 Priority: {str(note.get('priority', 'medium')).title()}\n\n"
        f"🔗 ดูรายละเอียด: {settings.WEB_PUBLIC_URL}/notes/{note['id']}"
    )
    results = [await send_telegram(chat_id, text) for chat_id in _note_chat_ids(note)]
    return bool(results) and all(results)


async def notify_stale(note: dict) -> bool:
    text = (
        "⚠️ โปรเจกต์ไม่มีความเคลื่อนไหว\n\n"
        f"📋 {note['title']}\n"
        f"ไม่ได้อัปเดตมากกว่า {settings.NOTIFY_STALE_DAYS} วัน\n"
        f"🔗 ดูรายละเอียด: {settings.WEB_PUBLIC_URL}/notes/{note['id']}"
    )
    results = [await send_telegram(chat_id, text) for chat_id in _note_chat_ids(note)]
    return bool(results) and all(results)


def format_digest(data: dict) -> str:
    today = date.today()
    start = today - timedelta(days=6)
    categories = data.get("by_category", {})
    completed = data.get("completed_this_week", [])
    active = data.get("active_projects", [])
    stale = data.get("stale_projects", [])
    lines = [
        "🧠 PARA Weekly Digest",
        f"{start.isoformat()} – {today.isoformat()}",
        "",
        "📊 สรุป:",
        f"  Notes ทั้งหมด: {data.get('total_notes', 0)}",
        f"  • Projects: {categories.get('projects', 0)}",
        f"  • Areas: {categories.get('areas', 0)}",
        f"  • Resources: {categories.get('resources', 0)}",
        f"  • Archives: {categories.get('archives', 0)}",
        "",
        "✅ เสร็จสิ้นสัปดาห์นี้:",
    ]
    lines.extend(f"  • {note['title']}" for note in completed[:10])
    if not completed:
        lines.append("  • —")
    lines.append("\n🔴 กำลังทำ:")
    lines.extend(f"  • {note['title']}" for note in active[:10])
    if not active:
        lines.append("  • —")
    lines.append(f"\n⚠️ Stale (ไม่อัปเดต > {settings.NOTIFY_STALE_DAYS} วัน):")
    lines.extend(f"  • {note['title']}" for note in stale[:10])
    if not stale:
        lines.append("  • —")
    lines.append(f"\n📝 Notes ใหม่ {data.get('new_notes_count', 0)} อันสัปดาห์นี้")
    return "\n".join(lines)


async def send_digest(digest_data: dict) -> bool:
    text = format_digest(digest_data)
    results = [await send_telegram(chat_id, text) for chat_id in notification_chat_ids()]
    return bool(results) and all(results)

