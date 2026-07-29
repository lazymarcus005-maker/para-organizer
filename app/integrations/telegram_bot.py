"""Telegram webhook update and command handling."""

from __future__ import annotations

import io
import json
import logging
import os
from datetime import date, timedelta
from typing import Any

import aiosqlite
import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.chat import chat_reply, clear_history, distill_note_from_history
from app.classifier import classify_note, extract_deadline_from_text
from app.config import settings
from app.database import get_connection
from app.models import PARA_CATEGORIES
from app.notifier import send_telegram
from app.utils import row_to_note, spawn_recurring_instance

logger = logging.getLogger("para.telegram")

CATEGORY_LABELS = {
    "inbox": "Inbox",
    "projects": "Projects",
    "areas": "Areas",
    "resources": "Resources",
    "archives": "Archives",
}
PRIORITY_ICONS = {"urgent": "🔴", "high": "🔴", "medium": "🟡", "low": "🟢"}
THAI_MONTHS = ("ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
               "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.")


def allowed_user_ids() -> set[int]:
    return {
        int(value.strip())
        for value in settings.TELEGRAM_ALLOWED_USERS.split(",")
        if value.strip().lstrip("-").isdigit()
    }


def format_date(value: str | date | None) -> str:
    if not value:
        return ""
    parsed = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return f"{parsed.day} {THAI_MONTHS[parsed.month - 1]} {parsed.year}"


def format_note_created(note: dict) -> str:
    category = CATEGORY_LABELS.get(note["para_category"], note["para_category"].title())
    priority = note.get("priority", "medium")
    lines = [
        "✅ บันทึกแล้ว!",
        f"📂 {category} · {PRIORITY_ICONS.get(priority, '🟡')} {priority.title()}",
    ]
    if note.get("deadline"):
        lines.append(f"📅 Deadline: {format_date(note['deadline'])}")
    if note.get("tags"):
        lines.append(f"🏷️ {', '.join(note['tags'])}")
    return "\n".join(lines)


async def _create_note(text: str, chat_id: int, message_id: int | None) -> dict:
    title = text.strip().splitlines()[0][:120]
    result = await classify_note(title, text)
    deadline = result.get("deadline")
    if not deadline:
        extracted = extract_deadline_from_text(text)
        deadline = extracted.isoformat() if extracted else None
    metadata = json.dumps(
        {"chat_id": chat_id, "message_id": message_id}, ensure_ascii=False
    )
    async with get_connection() as db:
        cursor = await db.execute(
            """INSERT INTO notes
               (title, content, para_category, sub_category, priority, deadline, tags,
                source, source_metadata, llm_model, llm_confidence, llm_reasoning)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'telegram', ?, ?, ?, ?)""",
            (
                title, text, result.get("para_category", "inbox"),
                result.get("sub_category"), result.get("priority", "medium"), deadline,
                json.dumps(result.get("tags", []), ensure_ascii=False), metadata,
                result.get("llm_model"), float(result.get("confidence", 0.0)),
                result.get("reasoning"),
            ),
        )
        note_id = cursor.lastrowid
        await db.execute(
            """INSERT INTO history (note_id, action, new_value, reason)
               VALUES (?, 'created', 'telegram', ?)""",
            (note_id, result.get("reasoning")),
        )
        await db.commit()
        row = await (await db.execute("SELECT * FROM notes WHERE id = ?", (note_id,))).fetchone()
        return row_to_note(row)


def _list_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Projects", callback_data="list:projects"),
            InlineKeyboardButton("Areas", callback_data="list:areas"),
        ],
        [
            InlineKeyboardButton("Resources", callback_data="list:resources"),
            InlineKeyboardButton("Archives", callback_data="list:archives"),
        ],
    ])


async def _list_notes(category: str | None) -> str:
    async with get_connection() as db:
        params: tuple[Any, ...] = ()
        where = "WHERE status != 'archived'"
        if category:
            where += " AND para_category = ?"
            params = (category,)
        rows = await (await db.execute(
            f"SELECT id, title, para_category, priority FROM notes {where} "
            "ORDER BY updated_at DESC LIMIT 20", params
        )).fetchall()
    if not rows:
        return "ยังไม่มีโน้ตในหมวดนี้"
    return "\n".join(
        f"#{row['id']} {PRIORITY_ICONS.get(row['priority'], '🟡')} "
        f"{row['title']} · {CATEGORY_LABELS.get(row['para_category'], row['para_category'])}"
        for row in rows
    )


async def _search(query: str) -> str:
    if not query:
        return "วิธีใช้: /search <คำค้น>"
    async with get_connection() as db:
        try:
            rows = await (await db.execute(
                """SELECT n.id, n.title, n.para_category FROM notes_fts f
                   JOIN notes n ON n.id = f.rowid
                   WHERE notes_fts MATCH ? ORDER BY bm25(notes_fts) LIMIT 10""",
                (query,),
            )).fetchall()
        except aiosqlite.OperationalError:
            rows = []
    if not rows:
        return "ไม่พบโน้ตที่ตรงกัน"
    return "\n".join(
        f"#{row['id']} {row['title']} · {CATEGORY_LABELS.get(row['para_category'], row['para_category'])}"
        for row in rows
    )


async def _deadlines() -> str:
    horizon = (date.today() + timedelta(days=14)).isoformat()
    async with get_connection() as db:
        rows = await (await db.execute(
            """SELECT id, title, deadline FROM notes
               WHERE status = 'active' AND deadline BETWEEN date('now') AND ?
               ORDER BY deadline""",
            (horizon,),
        )).fetchall()
    if not rows:
        return "ไม่มี deadline ใน 14 วันข้างหน้า"
    return "📅 Deadlines\n" + "\n".join(
        f"#{row['id']} {row['title']} — {format_date(row['deadline'])}" for row in rows
    )


async def _archive(note_id: str) -> str:
    if not note_id.isdigit():
        return "วิธีใช้: /done <id>"
    async with get_connection() as db:
        row = await (await db.execute("SELECT * FROM notes WHERE id = ?", (int(note_id),))).fetchone()
        if not row:
            return "ไม่พบโน้ต"
        note = row_to_note(row)
        await db.execute(
            """UPDATE notes SET status = 'archived', para_category = 'archives',
               archived_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (int(note_id),),
        )
        await db.execute(
            """INSERT INTO history (note_id, action, old_value, new_value)
               VALUES (?, 'archived', ?, 'archives')""",
            (int(note_id), row["para_category"]),
        )
        next_id = await spawn_recurring_instance(db, note)
        await db.commit()
    msg = f"✅ Archive โน้ต #{note_id} แล้ว"
    if next_id:
        msg += f"\n🔁 สร้างโน้ตถัดไป #{next_id} แล้ว"
    return msg


async def _move(args: list[str]) -> str:
    if len(args) != 2 or not args[0].isdigit() or args[1].lower() not in PARA_CATEGORIES:
        return "วิธีใช้: /move <id> <projects|areas|resources|archives>"
    note_id, category = int(args[0]), args[1].lower()
    async with get_connection() as db:
        row = await (await db.execute("SELECT para_category FROM notes WHERE id = ?", (note_id,))).fetchone()
        if not row:
            return "ไม่พบโน้ต"
        await db.execute(
            "UPDATE notes SET para_category = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (category, note_id),
        )
        await db.execute(
            """INSERT INTO history (note_id, action, old_value, new_value)
               VALUES (?, 'moved', ?, ?)""",
            (note_id, row["para_category"], category),
        )
        await db.commit()
    return f"✅ ย้ายโน้ต #{note_id} ไป {CATEGORY_LABELS[category]} แล้ว"


async def _stats() -> str:
    async with get_connection() as db:
        total = (await (await db.execute("SELECT COUNT(*) c FROM notes")).fetchone())["c"]
        rows = await (await db.execute(
            "SELECT para_category, COUNT(*) c FROM notes GROUP BY para_category"
        )).fetchall()
    counts = {row["para_category"]: row["c"] for row in rows}
    return "\n".join([
        f"📊 Notes ทั้งหมด: {total}",
        f"Projects: {counts.get('projects', 0)}",
        f"Areas: {counts.get('areas', 0)}",
        f"Resources: {counts.get('resources', 0)}",
        f"Archives: {counts.get('archives', 0)}",
    ])


async def _digest() -> str:
    from app.scheduler import build_digest
    from app.notifier import format_digest
    return format_digest(await build_digest())


HELP_TEXT = """🧠 PARA Organizer

💬 พิมพ์คุยกับบอทได้เลย (ไม่ต้องใส่ / ) — บอทจะตอบโดยอ้างอิงโน้ต PARA ของคุณ
ช่วยระดมความคิด วางแผน หรือถามอะไรก็ได้

/ask <คำถาม> — เริ่ม/ต่อบทสนทนากับบอท (เหมือนพิมพ์เฉย ๆ)
/note <ข้อความ> — เพิ่มโน้ตจากข้อความ
/note (ไม่ใส่ข้อความ) — สรุปบทสนทนาปัจจุบันเป็นโน้ต แล้วเริ่มบทสนทนาใหม่
/reset, /clear — ล้างประวัติบทสนทนา
/list [หมวด] — ดูรายการ
/search <คำค้น> — ค้นหา
/deadlines — deadline 14 วัน
/done <id> — archive โน้ต
/move <id> <หมวด> — ย้ายหมวด
/stats — สถิติ
/digest — สรุปรายสัปดาห์
/help — วิธีใช้"""


async def _note_from_conversation(chat_id: int, message_id: int | None) -> str:
    distilled = await distill_note_from_history(chat_id)
    if not distilled:
        return "ยังไม่มีบทสนทนาให้สรุปเป็นโน้ต ลองคุยกับบอทก่อน หรือใช้ /note <ข้อความ>"
    note = await _create_note(distilled, chat_id, message_id)
    await clear_history(chat_id)
    return format_note_created(note)


async def _handle_voice_message(chat_id: int, voice: dict) -> None:
    """Handle voice message: download, transcribe with Whisper, create note."""
    try:
        file_id = voice.get("file_id")
        if not file_id:
            await send_telegram(chat_id, "❌ Could not access voice file")
            return
        
        # Download file from Telegram
        from telegram import Bot
        async with Bot(settings.TELEGRAM_BOT_TOKEN) as bot:
            file_obj = await bot.get_file(file_id)
            file_data = await file_obj.download_as_bytearray()
        
        # Transcribe using Whisper via Claude Code / Hermes
        # Try to use local whisper or external API
        text = await _transcribe_audio(file_data)
        if not text:
            await send_telegram(chat_id, "❌ Could not transcribe voice message")
            return
        
        # Create note from transcribed text
        note = await _create_note(text, chat_id, None)
        await send_telegram(chat_id, format_note_created(note))
    except Exception:
        logger.exception("Failed to handle voice message")
        await send_telegram(chat_id, "❌ Error processing voice message")


async def _transcribe_audio(audio_bytes: bytearray) -> str | None:
    """Transcribe audio using Whisper API (OpenAI via Hermes)."""
    try:
        # Use OpenAI Whisper API if available, else try local whisper
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.warning("OPENAI_API_KEY not set; Whisper transcription unavailable")
            return None
        
        # Send to OpenAI Whisper API
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": ("audio.ogg", io.BytesIO(audio_bytes), "audio/ogg")},
                data={"model": "whisper-1"},
                timeout=60.0,
            )
            response.raise_for_status()
            result = response.json()
            return result.get("text", "").strip()
    except Exception:
        logger.exception("Failed to transcribe audio")
        return None


async def handle_text(text: str, chat_id: int, message_id: int | None = None) -> tuple[str, Any | None]:
    """Handle one text message and return reply text plus optional reply markup."""
    text = text.strip()
    if not text:
        return "พิมพ์คุยกับบอท หรือใช้ /help", None
    if not text.startswith("/"):
        return await chat_reply(chat_id, text), None

    command_token, _, raw_args = text.partition(" ")
    command = command_token.split("@", 1)[0].lower()
    raw_args = raw_args.strip()
    if command == "/note":
        if not raw_args:
            return await _note_from_conversation(chat_id, message_id), None
        note = await _create_note(raw_args, chat_id, message_id)
        await clear_history(chat_id)
        return format_note_created(note), None
    if command == "/ask":
        if not raw_args:
            return "วิธีใช้: /ask <คำถาม>", None
        return await chat_reply(chat_id, raw_args), None
    if command in ("/reset", "/clear"):
        await clear_history(chat_id)
        return "🗑️ ล้างประวัติการสนทนาแล้ว เริ่มคุยใหม่ได้เลย", None
    if command == "/list":
        category = raw_args.lower() or None
        if category and category not in PARA_CATEGORIES:
            return "หมวดไม่ถูกต้อง: projects, areas, resources, archives", None
        return await _list_notes(category), _list_keyboard()
    if command == "/search":
        return await _search(raw_args), None
    if command == "/deadlines":
        return await _deadlines(), None
    if command == "/done":
        return await _archive(raw_args), None
    if command == "/move":
        return await _move(raw_args.split()), None
    if command == "/stats":
        return await _stats(), None
    if command == "/digest":
        return await _digest(), None
    if command == "/help":
        return HELP_TEXT, None
    return "ไม่รู้จักคำสั่งนี้ ใช้ /help เพื่อดูคำสั่งทั้งหมด", None


async def handle_update(update: dict) -> None:
    """Process a Telegram update received by the webhook. Never raises."""
    try:
        await _handle_update(update)
    except Exception:
        logger.exception("Failed to process Telegram update: %r", update)


async def _handle_update(update: dict) -> None:
    if not isinstance(update, dict):
        logger.warning("Ignoring malformed Telegram update (not an object): %r", update)
        return

    callback = update.get("callback_query")
    if callback and isinstance(callback, dict):
        user_id = callback.get("from", {}).get("id")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        data = callback.get("data") or ""
        if chat_id is None or (allowed_user_ids() and user_id not in allowed_user_ids()):
            return
        if data.startswith("list:"):
            category = data.partition(":")[2]
            await send_telegram(chat_id, await _list_notes(category))
        elif data.startswith("stale:"):
            parts = data.split(":")
            if len(parts) == 3:
                action, note_id_str = parts[1], parts[2]
                try:
                    note_id = int(note_id_str)
                    async with get_connection() as db:
                        row = await (await db.execute(
                            "SELECT para_category FROM notes WHERE id = ?", (note_id,)
                        )).fetchone()
                        if not row:
                            await send_telegram(chat_id, "❌ Note not found")
                            return
                        
                        if action == "keep":
                            await db.execute(
                                "UPDATE notes SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (note_id,),
                            )
                            await db.execute(
                                "INSERT INTO history (note_id, action, new_value, reason) VALUES (?, 'keep_stale', ?, ?)",
                                (note_id, "active", "User confirmed project is still active"),
                            )
                            await send_telegram(chat_id, f"✅ Marked as active! Will remind you in {settings.NOTIFY_STALE_DAYS} days.")
                        elif action == "archive":
                            await db.execute(
                                "UPDATE notes SET status = 'archived', para_category = 'archives', archived_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (note_id,),
                            )
                            await db.execute(
                                "INSERT INTO history (note_id, action, old_value, new_value, reason) VALUES (?, 'archived', ?, 'archives', ?)",
                                (note_id, row["para_category"], "Archived from stale nudge"),
                            )
                            await send_telegram(chat_id, f"📦 Archived! You can restore it anytime.")
                        
                        await db.commit()
                except (ValueError, IndexError):
                    await send_telegram(chat_id, "❌ Invalid note ID")
        elif data.startswith("deadline:"):
            parts = data.split(":")
            if len(parts) == 4:
                action, days_str, note_id_str = parts[1], parts[2], parts[3]
                try:
                    note_id = int(note_id_str)
                    async with get_connection() as db:
                        row = await (await db.execute(
                            "SELECT deadline, status FROM notes WHERE id = ?", (note_id,)
                        )).fetchone()
                        if not row:
                            await send_telegram(chat_id, "❌ Note not found")
                            return

                        if action == "snooze":
                            days = int(days_str)
                            old_deadline = row["deadline"]
                            new_deadline = (date.fromisoformat(str(old_deadline)[:10]) + timedelta(days=days)).isoformat()
                            await db.execute(
                                "UPDATE notes SET deadline = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (new_deadline, note_id),
                            )
                            await db.execute(
                                "INSERT INTO history (note_id, action, old_value, new_value, reason) VALUES (?, 'deadline_snoozed', ?, ?, ?)",
                                (note_id, old_deadline, new_deadline, f"Snoozed +{days}d from Telegram"),
                            )
                            await db.commit()
                            await send_telegram(chat_id, f"📅 เลื่อน deadline #{note_id} → {format_date(new_deadline)}")
                        elif action == "done":
                            note_row = await (await db.execute(
                                "SELECT * FROM notes WHERE id = ?", (note_id,)
                            )).fetchone()
                            note = row_to_note(note_row)
                            await db.execute(
                                "UPDATE notes SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (note_id,),
                            )
                            await db.execute(
                                "INSERT INTO history (note_id, action, new_value, reason) VALUES (?, 'completed', 'completed', 'Marked done from Telegram deadline reminder')",
                                (note_id,),
                            )
                            next_id = await spawn_recurring_instance(db, note)
                            await db.commit()
                            msg = f"✅ โน้ต #{note_id} เสร็จสิ้นแล้ว!"
                            if next_id:
                                msg += f"\n🔁 สร้างโน้ตถัดไป #{next_id} แล้ว"
                            await send_telegram(chat_id, msg)
                except (ValueError, IndexError):
                    await send_telegram(chat_id, "❌ Invalid data")
        return

    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id", chat_id)
    if chat_id is None:
        return
    if allowed_user_ids() and user_id not in allowed_user_ids():
        logger.warning("Ignoring Telegram update from unauthorized user %s", user_id)
        return
    
    # Handle voice messages (IMP-13)
    voice = message.get("voice")
    if voice and isinstance(voice, dict):
        await _handle_voice_message(chat_id, voice)
        return
    
    # Handle text messages
    if not isinstance(message.get("text"), str):
        return
    reply, markup = await handle_text(message["text"], chat_id, message.get("message_id"))
    await send_telegram(chat_id, reply, reply_markup=markup)

