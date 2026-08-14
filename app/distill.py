"""Note distillation: summarize a long note into a 1-line ``summary`` field when
it gets archived. The LLM call goes through ``app.classifier.call_ollama`` so
the same model + task accounting is reused.

`distill_note` works against either backend:
- ``aiosqlite.Connection`` (SQLite, local-dev) — reads via the connection and
  updates via raw SQL.
- SQLAlchemy ``AsyncSession`` (PostgreSQL, production) — uses the ORM.
"""

from __future__ import annotations

import json
import logging

from app.classifier import call_ollama
from app.config import settings
from app.models_v2 import Note as PgNote

logger = logging.getLogger("para.distill")

DISTILL_SYSTEM_PROMPT = (
    "You are a note summarizer. Generate a concise 1-line summary of the "
    "following note in the same language as the note content. Keep it under "
    "100 characters."
)


def _is_async_session(db) -> bool:
    return hasattr(db, "add") and hasattr(db, "flush") and not hasattr(db, "execute_fetchall")


async def distill_note(db, note_id: int) -> str | None:
    """Distill ``note_id`` and persist the 1-line summary on the note.

    Returns the summary text, or None if the note could not be found or
    the LLM produced an empty response. Never raises.
    """
    is_pg = _is_async_session(db)

    if is_pg:
        note = await db.get(PgNote, note_id)
    else:
        cursor = await db.execute("SELECT id, title, content FROM notes WHERE id = ?", (note_id,))
        note = await cursor.fetchone()

    if note is None:
        logger.warning("Note %d not found for distillation", note_id)
        return None

    title = getattr(note, "title", None) or note["title"]
    content = getattr(note, "content", None) or note["content"]

    messages = [
        {"role": "system", "content": DISTILL_SYSTEM_PROMPT},
        {"role": "user", "content": f"ชื่อ: {title}\n\nเนื้อหา:\n{content}"},
    ]
    try:
        summary = await call_ollama(
            settings.CHAT_MODEL, messages=messages, format=None, task="distill"
        )
    except Exception:
        logger.warning("Distillation LLM call failed for note %s", note_id, exc_info=True)
        return None

    summary = (summary or "").strip() if isinstance(summary, str) else None
    if not summary:
        logger.warning("Distillation returned empty for note %s", note_id)
        return None

    if is_pg:
        note.summary = summary
        # Caller is responsible for committing the session.
    else:
        await db.execute(
            "UPDATE notes SET summary = ? WHERE id = ?",
            (summary, note_id),
        )

    logger.info("Note %s distilled: %s", note_id, summary[:60])
    return summary


# Re-export for callers that imported these symbols (keep module surface stable).
__all__ = ["DISTILL_SYSTEM_PROMPT", "distill_note"]


# Silence "imported but unused" for json — it remains part of the public module
# surface in case future code wants to log structured payloads.
_ = json
