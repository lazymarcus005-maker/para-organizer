"""Telegram webhook update and command handling."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.classifier import classify_note, extract_deadline_from_text
from app.config import settings
from app.database import get_connection
from app.models import PARA_CATEGORIES
from app.notifier import send_telegram
from app.utils import row_to_note

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
        row = await (await db.execute("SELECT para_category FROM notes WHERE id = ?", (int(note_id),))).fetchone()
        if not row:
            return "ไม่พบโน้ต"
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
        await db.commit()
    return f"✅ Archive โน้ต #{note_id} แล้ว"


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
/note <ข้อความ> — เพิ่มโน้ต
/list [หมวด] — ดูรายการ
/search <คำค้น> — ค้นหา
/deadlines — deadline 14 วัน
/done <id> — archive โน้ต
/move <id> <หมวด> — ย้ายหมวด
/stats — สถิติ
/digest — สรุปรายสัปดาห์
/help — วิธีใช้"""


async def handle_text(text: str, chat_id: int, message_id: int | None = None) -> tuple[str, Any | None]:
    """Handle one text message and return reply text plus optional reply markup."""
    text = text.strip()
    if not text:
        return "ส่งข้อความเพื่อสร้างโน้ต หรือใช้ /help", None
    if not text.startswith("/"):
        note = await _create_note(text, chat_id, message_id)
        return format_note_created(note), None

    command_token, _, raw_args = text.partition(" ")
    command = command_token.split("@", 1)[0].lower()
    raw_args = raw_args.strip()
    if command == "/note":
        if not raw_args:
            return "วิธีใช้: /note <ข้อความ>", None
        note = await _create_note(raw_args, chat_id, message_id)
        return format_note_created(note), None
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
    """Process a Telegram update received by the webhook."""
    callback = update.get("callback_query")
    if callback:
        user_id = callback.get("from", {}).get("id")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        data = callback.get("data", "")
        if chat_id is None or (allowed_user_ids() and user_id not in allowed_user_ids()):
            return
        if data.startswith("list:"):
            category = data.partition(":")[2]
            await send_telegram(chat_id, await _list_notes(category))
        return

    message = update.get("message") or update.get("edited_message")
    if not message or not isinstance(message.get("text"), str):
        return
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id", chat_id)
    if chat_id is None:
        return
    if allowed_user_ids() and user_id not in allowed_user_ids():
        logger.warning("Ignoring Telegram update from unauthorized user %s", user_id)
        return
    reply, markup = await handle_text(message["text"], chat_id, message.get("message_id"))
    await send_telegram(chat_id, reply, reply_markup=markup)

