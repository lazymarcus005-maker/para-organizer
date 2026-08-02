"""SB-08 — Feedback loop.

Records user corrections to LLM classifications, analyzes feedback patterns
(accuracy, common corrections), and serves recent corrections as few-shot examples
so the classifier can learn from past mistakes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import aiosqlite

from app.database import get_connection

logger = logging.getLogger("para.feedback")

_SNIPPET_LEN = 140


async def record_feedback(db: aiosqlite.Connection, note_id: int, field: str,
                          llm_value: str | None, user_value: str) -> dict:
    """Insert a feedback row and return it. Uses the caller's connection so the
    write participates in the caller's transaction."""
    cursor = await db.execute(
        "INSERT INTO feedback (note_id, field, llm_value, user_value) VALUES (?, ?, ?, ?)",
        (note_id, field, llm_value, user_value),
    )
    feedback_id = cursor.lastrowid
    row = await (await db.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,))).fetchone()
    return dict(row)


async def get_feedback_stats(days: int = 30) -> dict:
    """Analyze feedback patterns over the last `days` days."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(sep=" ")

    async with get_connection() as db:
        total = (await (await db.execute(
            "SELECT COUNT(*) c FROM feedback WHERE timestamp >= ?", (cutoff,)
        )).fetchone())["c"]

        field_rows = await (await db.execute(
            """SELECT field, COUNT(*) c FROM feedback
               WHERE timestamp >= ? GROUP BY field""",
            (cutoff,),
        )).fetchall()

        accuracy_rows = await (await db.execute(
            """SELECT llm_value,
                      COUNT(*) total,
                      SUM(CASE WHEN llm_value = user_value THEN 1 ELSE 0 END) correct
               FROM feedback
               WHERE field = 'para_category' AND timestamp >= ?
               GROUP BY llm_value""",
            (cutoff,),
        )).fetchall()

        correction_rows = await (await db.execute(
            """SELECT field, llm_value AS from_value, user_value AS to_value, COUNT(*) c
               FROM feedback
               WHERE timestamp >= ?
               GROUP BY field, llm_value, user_value
               ORDER BY c DESC LIMIT 10""",
            (cutoff,),
        )).fetchall()

    by_field = {row["field"]: row["c"] for row in field_rows}

    accuracy_by_category: dict[str, dict] = {}
    for row in accuracy_rows:
        category = row["llm_value"]
        if category is None:
            continue
        total_cat = row["total"] or 0
        correct = row["correct"] or 0
        accuracy_by_category[category] = {
            "total": total_cat,
            "correct": correct,
            "accuracy": round(correct / total_cat, 3) if total_cat else 0.0,
        }

    common_corrections = [
        {"field": row["field"], "from_value": row["from_value"], "to_value": row["to_value"], "count": row["c"]}
        for row in correction_rows
    ]

    suggestions: list[str] = []
    for corr in common_corrections:
        if corr["from_value"] is not None and corr["from_value"] != corr["to_value"]:
            suggestions.append(
                f"LLM มักทำนาย '{corr['field']}' เป็น '{corr['from_value']}' "
                f"แต่ควรเป็น '{corr['to_value']}' — เพิ่ม example ใน prompt"
            )
        if len(suggestions) >= 5:
            break

    return {
        "period_days": days,
        "total_feedback": total,
        "by_field": by_field,
        "accuracy_by_category": accuracy_by_category,
        "common_corrections": common_corrections,
        "suggestions": suggestions,
    }


async def get_few_shot_examples(field: str, limit: int = 3) -> list[dict]:
    """Return recent feedback entries for a field (joined with note content) to use
    as few-shot examples in classification prompts."""
    async with get_connection() as db:
        rows = await (await db.execute(
            """SELECT f.llm_value, f.user_value, n.content
               FROM feedback f
               JOIN notes n ON n.id = f.note_id
               WHERE f.field = ?
               ORDER BY f.timestamp DESC
               LIMIT ?""",
            (field, limit),
        )).fetchall()

    examples = []
    for row in rows:
        content = row["content"] or ""
        examples.append({
            "note_content_snippet": content[:_SNIPPET_LEN],
            "llm_value": row["llm_value"],
            "correct_value": row["user_value"],
        })
    return examples
