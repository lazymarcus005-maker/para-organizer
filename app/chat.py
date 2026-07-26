"""Conversational chat engine: RAG-grounded replies over PARA notes, with
conversation history persisted per Telegram chat_id in SQLite."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

import aiosqlite
import httpx

from app.classifier import call_ollama
from app.config import settings
from app.database import get_connection
from app.embed import embed_text
from app.vector_store import semantic_search

logger = logging.getLogger("para.chat")

DISTILL_SYSTEM_PROMPT = (
    "จากบทสนทนาด้านล่าง ให้สรุปเป็นเนื้อหาโน้ตหนึ่งชิ้นที่กระชับและชัดเจน สำหรับบันทึกลงระบบ PARA "
    "ตอบเป็นข้อความล้วน (ไม่ใช่ JSON) บรรทัดแรกคือหัวข้อสั้น ๆ ตามด้วยรายละเอียดที่จำเป็น "
    "ไม่ต้องใส่คำอธิบายอื่นนอกเหนือจากเนื้อหาโน้ต"
)

CHAT_FALLBACK_REPLY = "ขอโทษค่ะ ตอนนี้ระบบแชทขัดข้องชั่วคราว ลองใหม่อีกครั้งนะคะ"


async def append_message(chat_id: int, role: str, content: str) -> None:
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO chat_messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, role, content),
        )
        await db.execute(
            """DELETE FROM chat_messages WHERE chat_id = ? AND id NOT IN (
                   SELECT id FROM chat_messages WHERE chat_id = ?
                   ORDER BY id DESC LIMIT ?
               )""",
            (chat_id, chat_id, settings.CHAT_HISTORY_MAX),
        )
        await db.commit()


async def get_history(chat_id: int) -> list[dict]:
    """Last CHAT_HISTORY_MAX messages for chat_id, oldest first."""
    async with get_connection() as db:
        rows = await (await db.execute(
            "SELECT role, content FROM chat_messages WHERE chat_id = ? ORDER BY id ASC LIMIT ?",
            (chat_id, settings.CHAT_HISTORY_MAX),
        )).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in rows]


async def clear_history(chat_id: int) -> None:
    async with get_connection() as db:
        await db.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        await db.commit()


def _build_fts_query(text: str) -> str:
    tokens = [t for t in re.findall(r"\w+", text) if len(t) >= 2]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens[:8])


async def _search_notes(db: aiosqlite.Connection, query: str, limit: int = 5) -> list:
    fts_query = _build_fts_query(query)
    if not fts_query:
        return []
    try:
        rows = await (await db.execute(
            """SELECT n.id, n.title, n.content, n.para_category FROM notes_fts f
               JOIN notes n ON n.id = f.rowid
               WHERE notes_fts MATCH ? ORDER BY bm25(notes_fts) LIMIT ?""",
            (fts_query, limit),
        )).fetchall()
    except aiosqlite.OperationalError:
        return []
    return rows


async def _upcoming_deadlines(db: aiosqlite.Connection, limit: int = 5) -> list:
    horizon = (date.today() + timedelta(days=14)).isoformat()
    rows = await (await db.execute(
        """SELECT id, title, deadline FROM notes
           WHERE status = 'active' AND deadline BETWEEN date('now') AND ?
           ORDER BY deadline LIMIT ?""",
        (horizon, limit),
    )).fetchall()
    return rows


async def _quick_stats(db: aiosqlite.Connection) -> str:
    total = (await (await db.execute("SELECT COUNT(*) c FROM notes")).fetchone())["c"]
    rows = await (await db.execute(
        "SELECT para_category, COUNT(*) c FROM notes GROUP BY para_category"
    )).fetchall()
    counts = {row["para_category"]: row["c"] for row in rows}
    return (
        f"สถิติ: ทั้งหมด {total} · Projects {counts.get('projects', 0)} · "
        f"Areas {counts.get('areas', 0)} · Resources {counts.get('resources', 0)} · "
        f"Archives {counts.get('archives', 0)}"
    )


async def _hybrid_retrieve(user_text: str, db: aiosqlite.Connection) -> list:
    """Merge keyword (FTS5/BM25) and semantic (vector) search into one ranked
    list of notes, weighted by RAG_HYBRID_RATIO. Falls back to FTS-only if
    semantic search is disabled or unavailable (embedding provider down, no
    vector index yet, etc.) — it never blocks keyword retrieval."""
    scores: dict[int, dict[str, float]] = {}

    if settings.RAG_HYBRID_ENABLED:
        try:
            query_embedding = await embed_text(user_text)
            if query_embedding:
                for note_id, score in await semantic_search(db, query_embedding, limit=settings.RAG_SEARCH_LIMIT):
                    scores.setdefault(note_id, {})["semantic"] = score
        except Exception:
            logger.warning("Semantic search failed, falling back to FTS only", exc_info=True)

    fts_matches = await _search_notes(db, user_text, limit=settings.RAG_SEARCH_LIMIT)
    for rank, row in enumerate(fts_matches):
        scores.setdefault(row["id"], {})["fts"] = 1.0 / (rank + 1.0)

    if not scores:
        return []

    ratio = settings.RAG_HYBRID_RATIO
    for note_scores in scores.values():
        note_scores["combined"] = (
            ratio * note_scores.get("semantic", 0.0) + (1 - ratio) * note_scores.get("fts", 0.0)
        )

    top_ids = sorted(scores, key=lambda nid: scores[nid]["combined"], reverse=True)[:settings.RAG_SEARCH_LIMIT]

    placeholders = ",".join("?" * len(top_ids))
    rows = await (await db.execute(
        f"SELECT id, title, content, para_category FROM notes WHERE id IN ({placeholders})",
        top_ids,
    )).fetchall()
    by_id = {row["id"]: row for row in rows}
    return [by_id[nid] for nid in top_ids if nid in by_id]


async def _build_context(user_text: str) -> str:
    """Compact RAG summary: matching notes, near-term deadlines, and quick stats."""
    async with get_connection() as db:
        matched = await _hybrid_retrieve(user_text, db)
        deadlines = await _upcoming_deadlines(db)
        stats = await _quick_stats(db)

    lines: list[str] = []
    if matched:
        lines.append("โน้ตที่เกี่ยวข้อง:")
        lines.extend(
            f"- #{row['id']} [{row['para_category']}] {row['title']}: {(row['content'] or '')[:200]}"
            for row in matched
        )
    if deadlines:
        lines.append("Deadline ที่ใกล้ถึง:")
        lines.extend(
            f"- #{row['id']} {row['title']} — {row['deadline']}" for row in deadlines
        )
    lines.append(stats)
    return "\n".join(lines)


async def chat_reply(chat_id: int, user_text: str) -> str:
    """Answer user_text conversationally, grounded in PARA notes, and persist the turn."""
    context = await _build_context(user_text)
    history = await get_history(chat_id)

    messages = [{"role": "system", "content": settings.CHAT_SYSTEM_PROMPT}]
    if context:
        messages.append({"role": "system", "content": f"บริบทจากโน้ต PARA ของผู้ใช้:\n{context}"})
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    try:
        reply = (await call_ollama(settings.CHAT_MODEL, messages=messages, format=None)).strip()
    except (httpx.TimeoutException, httpx.HTTPError) as e:
        logger.warning("Chat model call failed: %s", e)
        reply = CHAT_FALLBACK_REPLY

    await append_message(chat_id, "user", user_text)
    await append_message(chat_id, "assistant", reply)
    return reply


async def distill_note_from_history(chat_id: int) -> str | None:
    """Ask the chat model to summarize the conversation into a single note's content."""
    history = await get_history(chat_id)
    if not history:
        return None

    messages = (
        [{"role": "system", "content": DISTILL_SYSTEM_PROMPT}]
        + history
        + [{"role": "user", "content": "สรุปบทสนทนาข้างต้นเป็นเนื้อหาโน้ตหนึ่งชิ้น"}]
    )
    try:
        result = await call_ollama(settings.CHAT_MODEL, messages=messages, format=None)
    except (httpx.TimeoutException, httpx.HTTPError) as e:
        logger.warning("Failed to distill note from chat history for chat %s: %s", chat_id, e)
        return None
    return result.strip() or None
