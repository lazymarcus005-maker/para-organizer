"""Centralized LLM usage logging and aggregation (the `llm_usage` table).

`log_usage` is called automatically from `call_ollama` (see app.classifier) after
every successful LLM call, so callers get token accounting for free. It is strictly
best-effort — any failure is swallowed so logging never breaks an LLM call.
"""

from __future__ import annotations

import logging

from app.database import get_connection

logger = logging.getLogger("para.usage")


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
    try:
        async with get_connection() as db:
            await db.execute(
                """INSERT INTO llm_usage
                       (model, task, prompt_tokens, completion_tokens, note_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (model, task, prompt_tokens, completion_tokens, note_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — logging must never break the caller
        logger.warning(
            "Failed to log LLM usage (model=%s task=%s)", model, task, exc_info=True
        )


def _bucket(prompt: int | None, completion: int | None, calls: int) -> dict:
    return {
        "prompt_tokens": prompt or 0,
        "completion_tokens": completion or 0,
        "calls": calls,
    }


async def usage_summary(days: int = 7) -> dict:
    """Aggregate llm_usage over the last `days` days.

    Returns totals plus breakdowns by model, by task, and by day.
    """
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
