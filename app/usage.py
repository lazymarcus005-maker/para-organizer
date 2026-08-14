"""Centralized LLM usage logging and aggregation (the `llm_usage` table).

`log_usage` is called automatically from `call_ollama` (see app.classifier) after
every successful LLM call, so callers get token accounting for free. It is strictly
best-effort — any failure is swallowed so logging never breaks an LLM call.

Supports both backends:
- PostgreSQL (production): writes via SQLAlchemy async session when one is
  supplied, otherwise opens its own via ``async_session_factory``.
- SQLite (local-dev / legacy): falls back to ``app.database.get_connection``.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from app.config import settings
from app.database_v2 import async_session_factory
from app.models_v2 import LlmUsage

logger = logging.getLogger("para.usage")


def _using_postgres() -> bool:
    return bool(settings.PARA_DB_URL and "postgresql" in settings.PARA_DB_URL)


async def _log_usage_pg(model: str, task: str, prompt_tokens: int | None,
                        completion_tokens: int | None, note_id: int | None) -> None:
    try:
        async with async_session_factory() as session:
            session.add(LlmUsage(
                model=model,
                task=task,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                note_id=note_id,
            ))
            await session.commit()
    except Exception:
        logger.warning(
            "Failed to log LLM usage (model=%s task=%s)", model, task, exc_info=True
        )


async def _log_usage_sqlite(model: str, task: str, prompt_tokens: int | None,
                            completion_tokens: int | None, note_id: int | None) -> None:
    from app.database import get_connection
    try:
        async with get_connection() as db:
            await db.execute(
                """INSERT INTO llm_usage
                       (model, task, prompt_tokens, completion_tokens, note_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (model, task, prompt_tokens, completion_tokens, note_id),
            )
            await db.commit()
    except Exception:
        logger.warning(
            "Failed to log LLM usage (model=%s task=%s)", model, task, exc_info=True
        )


async def log_usage(
    model: str,
    task: str,
    usage_dict: dict | None,
    note_id: int | None = None,
) -> None:
    """Record one LLM call in the llm_usage table. Never raises.

    Args:
        model: e.g. 'deepseek-v4-flash', 'gpt-oss:20b'.
        task: one of 'classify', 'chat', 'review', 'distill'.
        usage_dict: LLM response usage block, e.g.
            {'prompt_tokens': X, 'completion_tokens': Y}. May be None/partial.
        note_id: optional link to a note.
    """
    usage_dict = usage_dict or {}
    prompt_tokens = usage_dict.get("prompt_tokens")
    completion_tokens = usage_dict.get("completion_tokens")

    if _using_postgres():
        await _log_usage_pg(model, task, prompt_tokens, completion_tokens, note_id)
    else:
        await _log_usage_sqlite(model, task, prompt_tokens, completion_tokens, note_id)


def _bucket(prompt: int | None, completion: int | None, calls: int) -> dict:
    return {
        "prompt_tokens": prompt or 0,
        "completion_tokens": completion or 0,
        "calls": calls,
    }


async def _usage_summary_pg(days: int) -> dict:
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with async_session_factory() as session:
        total_row = (await session.execute(
            select(
                func.coalesce(func.sum(LlmUsage.prompt_tokens), 0).label("p"),
                func.coalesce(func.sum(LlmUsage.completion_tokens), 0).label("c"),
                func.count().label("n"),
            ).where(LlmUsage.ts >= since)
        )).one()
        model_rows = (await session.execute(
            select(
                LlmUsage.model,
                func.coalesce(func.sum(LlmUsage.prompt_tokens), 0).label("p"),
                func.coalesce(func.sum(LlmUsage.completion_tokens), 0).label("c"),
                func.count().label("n"),
            ).where(LlmUsage.ts >= since).group_by(LlmUsage.model)
        )).all()
        task_rows = (await session.execute(
            select(
                LlmUsage.task,
                func.coalesce(func.sum(LlmUsage.prompt_tokens), 0).label("p"),
                func.coalesce(func.sum(LlmUsage.completion_tokens), 0).label("c"),
                func.count().label("n"),
            ).where(LlmUsage.ts >= since).group_by(LlmUsage.task)
        )).all()
        # Group by calendar day in UTC. PostgreSQL: date_trunc('day', ts).
        from sqlalchemy import text as sa_text
        day_rows = (await session.execute(
            select(
                func.date_trunc("day", LlmUsage.ts).label("day"),
                func.coalesce(func.sum(LlmUsage.prompt_tokens), 0).label("p"),
                func.coalesce(func.sum(LlmUsage.completion_tokens), 0).label("c"),
                func.count().label("n"),
            ).where(LlmUsage.ts >= since).group_by(sa_text("day")).order_by(sa_text("day"))
        )).all()

    return {
        "days": days,
        "total_prompt_tokens": int(total_row.p or 0),
        "total_completion_tokens": int(total_row.c or 0),
        "total_calls": int(total_row.n or 0),
        "by_model": {
            row.model: _bucket(int(row.p or 0), int(row.c or 0), int(row.n or 0))
            for row in model_rows
        },
        "by_task": {
            row.task: _bucket(int(row.p or 0), int(row.c or 0), int(row.n or 0))
            for row in task_rows
        },
        "by_day": [
            {"day": str(row.day.date()) if row.day else None,
             **_bucket(int(row.p or 0), int(row.c or 0), int(row.n or 0))}
            for row in day_rows
        ],
    }


async def _usage_summary_sqlite(days: int) -> dict:
    from app.database import get_connection
    since = f"-{int(days)} days"
    async with get_connection() as db:
        total_row = await (await db.execute(
            """SELECT COALESCE(SUM(prompt_tokens), 0) AS p,
                      COALESCE(SUM(completion_tokens), 0) AS c,
                      COUNT(*) AS n
               FROM llm_usage WHERE ts >= datetime('now', ?)""",
            (since,),
        )).fetchone()

        model_rows = await (await db.execute(
            """SELECT model,
                      COALESCE(SUM(prompt_tokens), 0) AS p,
                      COALESCE(SUM(completion_tokens), 0) AS c,
                      COUNT(*) AS n
               FROM llm_usage WHERE ts >= datetime('now', ?)
               GROUP BY model""",
            (since,),
        )).fetchall()

        task_rows = await (await db.execute(
            """SELECT task,
                      COALESCE(SUM(prompt_tokens), 0) AS p,
                      COALESCE(SUM(completion_tokens), 0) AS c,
                      COUNT(*) AS n
               FROM llm_usage WHERE ts >= datetime('now', ?)
               GROUP BY task""",
            (since,),
        )).fetchall()

        day_rows = await (await db.execute(
            """SELECT date(ts) AS day,
                      COALESCE(SUM(prompt_tokens), 0) AS p,
                      COALESCE(SUM(completion_tokens), 0) AS c,
                      COUNT(*) AS n
               FROM llm_usage WHERE ts >= datetime('now', ?)
               GROUP BY date(ts) ORDER BY day""",
            (since,),
        )).fetchall()

    return {
        "days": int(days),
        "total_prompt_tokens": total_row["p"],
        "total_completion_tokens": total_row["c"],
        "total_calls": total_row["n"],
        "by_model": {
            row["model"]: _bucket(row["p"], row["c"], row["n"]) for row in model_rows
        },
        "by_task": {
            row["task"]: _bucket(row["p"], row["c"], row["n"]) for row in task_rows
        },
        "by_day": [
            {"day": row["day"], **_bucket(row["p"], row["c"], row["n"])}
            for row in day_rows
        ],
    }


async def usage_summary(days: int = 7) -> dict:
    """Aggregate llm_usage over the last `days` days.

    Returns totals plus breakdowns by model, by task, and by day.
    """
    if _using_postgres():
        return await _usage_summary_pg(days)
    return await _usage_summary_sqlite(days)
