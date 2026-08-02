"""Task delegation service for Hermes agent coordination (SB-02)."""

import json
import logging

import aiosqlite

from app.classifier import classify_note
from app.config import settings

logger = logging.getLogger("para.tasks")

TASK_TYPES = ("general", "research", "code", "deploy", "review", "automation")


def _row_to_task(row) -> dict:
    return dict(row)


async def create_task(
    db: aiosqlite.Connection,
    note_id: int | None,
    prompt: str,
    task_type: str = "general",
    agent_id: str | None = None,
) -> dict:
    cursor = await db.execute(
        "INSERT INTO tasks (note_id, task_type, prompt, agent_id) VALUES (?, ?, ?, ?)",
        (note_id, task_type, prompt, agent_id),
    )
    task_id = cursor.lastrowid
    await db.commit()
    logger.info("Created task #%s (type=%s, note_id=%s)", task_id, task_type, note_id)
    return await get_task(db, task_id)


async def get_task(db: aiosqlite.Connection, task_id: int) -> dict | None:
    cursor = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_task(row)


async def list_tasks(
    db: aiosqlite.Connection,
    status: str | None = None,
    note_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict], int]:
    clauses: list[str] = []
    params: list = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if note_id is not None:
        clauses.append("note_id = ?")
        params.append(note_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    count_cursor = await db.execute(f"SELECT COUNT(*) AS c FROM tasks {where}", params)
    total = (await count_cursor.fetchone())["c"]

    cursor = await db.execute(
        f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    rows = await cursor.fetchall()
    return [_row_to_task(r) for r in rows], total


async def complete_task(db: aiosqlite.Connection, task_id: int, result: str) -> dict | None:
    task = await get_task(db, task_id)
    if task is None:
        return None

    await db.execute(
        "UPDATE tasks SET status = 'completed', result = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (result, task_id),
    )

    if task["note_id"] is not None:
        try:
            cls = await classify_note(f"Task #{task_id} result", result)
            para_category = cls.get("para_category", "inbox")
            sub_category = cls.get("sub_category")
            priority = cls.get("priority", "medium")
            deadline = cls.get("deadline")
            tags = cls.get("tags", [])
            llm_model = cls.get("llm_model")
            llm_confidence = float(cls.get("confidence", 0.0))
            llm_reasoning = cls.get("reasoning")

            cursor = await db.execute(
                """
                INSERT INTO notes (title, content, para_category, sub_category, priority, deadline,
                                    tags, source, llm_model, llm_confidence, llm_reasoning)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"Task #{task_id} result", result, para_category, sub_category, priority, deadline,
                    json.dumps(tags, ensure_ascii=False), f"task:{task_id}", llm_model, llm_confidence, llm_reasoning,
                ),
            )
            note_id = cursor.lastrowid
            await db.execute(
                "INSERT INTO history (note_id, action, new_value) VALUES (?, 'created', ?)",
                (note_id, f"task:{task_id}"),
            )
            logger.info("Created note #%s from task #%s result", note_id, task_id)
        except Exception:
            logger.warning("Failed to create note from task #%s result", task_id, exc_info=True)

    await db.commit()
    logger.info("Completed task #%s", task_id)
    return await get_task(db, task_id)


async def fail_task(db: aiosqlite.Connection, task_id: int, reason: str) -> dict | None:
    task = await get_task(db, task_id)
    if task is None:
        return None

    await db.execute(
        "UPDATE tasks SET status = 'failed', result = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (reason, task_id),
    )
    await db.commit()
    logger.info("Failed task #%s: %s", task_id, reason)
    return await get_task(db, task_id)


def detect_action_verbs(text: str) -> list[str]:
    verbs = [v.strip() for v in settings.TASK_ACTION_VERBS.split(",") if v.strip()]
    return [v for v in verbs if v in text]


async def suggest_task_from_note(title: str, content: str) -> dict | None:
    if not settings.TASK_AUTO_EXTRACT:
        return None
    combined = f"{title} {content}"
    matched = detect_action_verbs(combined)
    if not matched:
        return None

    task_type = "general"
    if any(v in combined for v in ("deploy", "ติดตั้ง")):
        task_type = "deploy"
    elif any(v in combined for v in ("รัน", "ทดสอบ")):
        task_type = "code"
    elif any(v in combined for v in ("สร้าง",)):
        task_type = "automation"

    prompt = content if len(content) <= 200 else content[:200]
    logger.info("Suggested task from note content (verbs=%s)", matched)
    return {"prompt": prompt, "task_type": task_type, "matched_verbs": matched}
