"""SB-08 — Feedback loop.

Records user corrections to LLM classifications, analyzes feedback patterns
(accuracy, common corrections), and serves recent corrections as few-shot examples
so the classifier can learn from past mistakes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database_v2 import async_session_factory
from app.models_v2 import Feedback, Note

logger = logging.getLogger("para.feedback")

_SNIPPET_LEN = 140


def _row_to_dict(row: Feedback) -> dict:
    return {
        "id": row.id,
        "note_id": row.note_id,
        "field": row.field,
        "llm_value": row.llm_value,
        "user_value": row.user_value,
        "timestamp": row.created_at.isoformat() if row.created_at else None,
    }


async def record_feedback(session: AsyncSession | None, note_id: int, field: str,
                          llm_value: str | None, user_value: str) -> dict:
    """Insert a feedback row and return it. Uses `session` if given (participates
    in the caller's transaction), otherwise opens and commits its own."""
    feedback = Feedback(note_id=note_id, field=field, llm_value=llm_value, user_value=user_value)

    if session is not None:
        session.add(feedback)
        await session.flush()
        await session.refresh(feedback)
    else:
        async with async_session_factory() as owned:
            owned.add(feedback)
            await owned.commit()
            await owned.refresh(feedback)

    return _row_to_dict(feedback)


async def get_feedback_stats(days: int = 30) -> dict:
    """Analyze feedback patterns over the last `days` days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with async_session_factory() as session:
        total = (await session.execute(
            select(func.count()).select_from(Feedback).where(Feedback.created_at >= cutoff)
        )).scalar_one()

        field_rows = (await session.execute(
            select(Feedback.field, func.count().label("c"))
            .where(Feedback.created_at >= cutoff)
            .group_by(Feedback.field)
        )).all()

        accuracy_rows = (await session.execute(
            select(
                Feedback.llm_value,
                func.count().label("total"),
                func.sum(
                    case((Feedback.llm_value == Feedback.user_value, 1), else_=0)
                ).label("correct"),
            )
            .where(Feedback.field == "para_category", Feedback.created_at >= cutoff)
            .group_by(Feedback.llm_value)
        )).all()

        correction_rows = (await session.execute(
            select(
                Feedback.field,
                Feedback.llm_value.label("from_value"),
                Feedback.user_value.label("to_value"),
                func.count().label("c"),
            )
            .where(Feedback.created_at >= cutoff)
            .group_by(Feedback.field, Feedback.llm_value, Feedback.user_value)
            .order_by(func.count().desc())
            .limit(10)
        )).all()

    by_field = {row.field: row.c for row in field_rows}

    accuracy_by_category: dict[str, dict] = {}
    for row in accuracy_rows:
        category = row.llm_value
        if category is None:
            continue
        total_cat = row.total or 0
        correct = row.correct or 0
        accuracy_by_category[category] = {
            "total": total_cat,
            "correct": correct,
            "accuracy": round(correct / total_cat, 3) if total_cat else 0.0,
        }

    common_corrections = [
        {"field": row.field, "from_value": row.from_value, "to_value": row.to_value, "count": row.c}
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
    async with async_session_factory() as session:
        rows = (await session.execute(
            select(Feedback.llm_value, Feedback.user_value, Note.content)
            .join(Note, Note.id == Feedback.note_id)
            .where(Feedback.field == field)
            .order_by(Feedback.created_at.desc())
            .limit(limit)
        )).all()

    examples = []
    for row in rows:
        content = row.content or ""
        examples.append({
            "note_content_snippet": content[:_SNIPPET_LEN],
            "llm_value": row.llm_value,
            "correct_value": row.user_value,
        })
    return examples
