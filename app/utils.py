"""Shared helpers for converting DB rows to API-friendly dicts.

`row_to_note` and `compute_next_deadline` are pure functions — no DB coupling.
`spawn_recurring_instance` works against either a SQLite ``aiosqlite.Connection``
or a SQLAlchemy ``AsyncSession`` (production / PostgreSQL). The caller picks the
backend and passes the connection; this keeps the helper transport-agnostic
and avoids spinning up a second connection inside a scheduled job.
"""

import json
import logging
from datetime import date, timedelta

from app.config import settings
from app.models_v2 import History as PgHistory
from app.models_v2 import Note as PgNote

logger = logging.getLogger("para.utils")


def row_to_note(row) -> dict:
    d = dict(row)
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["tags"] = []
    try:
        d["source_metadata"] = json.loads(d.get("source_metadata") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["source_metadata"] = {}
    # A note is flagged for review when it was classified by the LLM but the
    # model wasn't confident enough (see classifier._apply_confidence_routing,
    # which also routes such notes to the inbox). Manually created notes
    # (llm_model is None) are never flagged.
    try:
        confidence = float(d.get("llm_confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    d["review_needed"] = (
        d.get("llm_model") is not None
        and confidence < settings.RECLASSIFY_CONFIDENCE_THRESHOLD
    )
    return d


def compute_next_deadline(current_deadline: str, recurrence: dict) -> str | None:
    freq = recurrence.get("freq", "weekly")
    interval = int(recurrence.get("interval", 1))
    try:
        base = date.fromisoformat(str(current_deadline)[:10])
    except (ValueError, TypeError):
        base = date.today()
    if freq == "daily":
        return (base + timedelta(days=interval)).isoformat()
    if freq == "weekly":
        return (base + timedelta(weeks=interval)).isoformat()
    if freq == "monthly":
        month = base.month + interval
        year = base.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        day = min(base.day, [31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
        return date(year, month, day).isoformat()
    return None


def _normalize_recurrence(recurrence_raw) -> dict | None:
    if not recurrence_raw:
        return None
    if isinstance(recurrence_raw, str):
        try:
            return json.loads(recurrence_raw)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(recurrence_raw, dict):
        return recurrence_raw
    return None


async def spawn_recurring_instance(db, note: dict) -> int | None:
    """If the note has a valid recurrence config, create the next instance.

    Works with both backends:
    - ``aiosqlite.Connection`` (SQLite, local-dev) — uses raw SQL via the
      passed-in connection; relies on the caller for ``commit()``.
    - SQLAlchemy ``AsyncSession`` (PostgreSQL, production) — adds the new
      ``Note`` and ``History`` row, then ``flush()`` so the caller can
      ``commit()`` in the same transaction it already manages.

    Returns the new note id, or None if not recurring.
    """
    recurrence = _normalize_recurrence(note.get("recurrence"))
    if recurrence is None:
        return None

    next_deadline = compute_next_deadline(
        note.get("deadline") or date.today().isoformat(), recurrence
    )
    if not next_deadline:
        return None

    # Detect backend by connection class. AsyncSession is the only async-session
    # type we use in production; aiosqlite.Connection is the legacy adapter.
    is_async_session = hasattr(db, "add") and hasattr(db, "flush") and not hasattr(db, "execute_fetchall")

    if is_async_session:
        new_note = PgNote(
            title=note["title"],
            content=note["content"],
            para_category=note.get("para_category", "inbox"),
            sub_category=note.get("sub_category"),
            priority=note.get("priority", "medium"),
            deadline=date.fromisoformat(next_deadline),
            tags=note.get("tags") or [],
            source="recurring",
            recurrence=recurrence,
            llm_model=note.get("llm_model"),
            llm_confidence=float(note.get("llm_confidence") or 0.0),
            llm_reasoning=note.get("llm_reasoning"),
        )
        db.add(new_note)
        await db.flush()
        new_id = new_note.id
        db.add(PgHistory(
            note_id=new_id,
            action="created",
            new_value="recurring",
            reason=f"Recurring instance from #{note['id']}",
        ))
        await db.flush()
        logger.info("Spawned recurring instance #%s from #%s, deadline %s", new_id, note["id"], next_deadline)
        return new_id

    # SQLite / aiosqlite path
    cursor = await db.execute(
        """INSERT INTO notes (title, content, para_category, sub_category, priority, deadline,
                              tags, source, recurrence, llm_model, llm_confidence, llm_reasoning)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'recurring', ?, ?, ?, ?)""",
        (
            note["title"], note["content"], note["para_category"], note.get("sub_category"),
            note.get("priority", "medium"), next_deadline,
            json.dumps(note.get("tags") or [], ensure_ascii=False),
            json.dumps(recurrence, ensure_ascii=False),
            note.get("llm_model"), note.get("llm_confidence", 0.0), note.get("llm_reasoning"),
        ),
    )
    new_id = cursor.lastrowid
    await db.execute(
        "INSERT INTO history (note_id, action, new_value, reason) VALUES (?, 'created', 'recurring', ?)",
        (new_id, f"Recurring instance from #{note['id']}"),
    )
    logger.info("Spawned recurring instance #%s from #%s, deadline %s", new_id, note["id"], next_deadline)
    return new_id
