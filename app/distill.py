"""Note distillation: generate 1-line summaries for archived notes."""

import logging

import aiosqlite

from app.classifier import call_ollama
from app.config import settings

logger = logging.getLogger("para.distill")

DISTILL_SYSTEM_PROMPT = (
    "คุณจะได้รับชื่อและเนื้อหาของโน้ตหนึ่งชิ้น โปรดสรุปการเรียนรู้หรือสิ่งสำคัญ "
    "ของโน้ตนั้นเป็นบรรทัดเดียว (ประมาณ 10-20 คำ) ที่กระชับ ชัดเจน และเป็นประโยคสมบูรณ์ "
    "ไม่ใส่หัวข้อหรือหมายเลข เพียงแค่สารสำคัญ"
)


async def distill_note(db: aiosqlite.Connection, note_id: int) -> str | None:
    """Generate a 1-line summary for a note being archived.
    
    Args:
        db: Database connection
        note_id: ID of note to distill
        
    Returns:
        Generated summary string (1-2 lines), or None if generation fails.
    """
    # Fetch the note
    cursor = await db.execute(
        "SELECT title, content FROM notes WHERE id = ?",
        (note_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        logger.warning("Note %d not found for distillation", note_id)
        return None
    
    title = row["title"]
    content = row["content"]
    
    # Prepare message
    messages = [
        {"role": "system", "content": DISTILL_SYSTEM_PROMPT},
        {"role": "user", "content": f"ชื่อ: {title}\n\nเนื้อหา:\n{content}"},
    ]
    
    try:
        summary = await call_ollama(
            settings.CHAT_MODEL,
            messages=messages,
            format=None,
            task="distill",
        )
        return summary.strip() if summary else None
    except Exception as e:
        logger.warning("Failed to distill note %d: %s", note_id, e)
        return None
