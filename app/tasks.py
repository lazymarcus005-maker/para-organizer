"""Task delegation service for Hermes agent coordination (SB-02).

Each function works against either backend (auto-detected by the connection type):
- ``aiosqlite.Connection`` (SQLite, local-dev / legacy)
- SQLAlchemy ``AsyncSession`` (PostgreSQL, production)

`create_task` / `complete_task` / `fail_task` etc. return the new task as a
plain dict so callers can serialize without depending on the ORM.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import func, select

from app.classifier import classify_note
from app.config import settings
from app.models_v2 import History as PgHistory
from app.models_v2 import Note as PgNote
from app.models_v2 import Task as PgTask

logger = logging.getLogger("para.tasks")

TASK_TYPES = ("general", "research", "code", "deploy", "review", "automation")


def _is_async_session(db) -> bool:
    return hasattr(db, "add") and hasattr(db, "flush") and not hasattr(db, "execute_fetchall")


def _task_to_dict_pg(task: PgTask) -> dict:
    return {
        "id": task.id,
        "prompt": task.prompt,
        "status": task.status,
        "task_type": task.task_type,
        "result": task.result,
        "agent_id": task.agent_id,
        "hermes_job_id": task.hermes_job_id,
        "note_id": task.note_id,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


def _task_to_dict_sqlite(row) -> dict:
    d = dict(row)
    if "task_type" not in d:
        d["task_type"] = "general"
    return d


# ── Create ──────────────────────────────────────────────────────────────────


async def create_task(
    db,
    note_id: int | None,
    prompt: str,
    task_type: str = "general",
    agent_id: str | None = None,
) -> dict:
    if task_type not in TASK_TYPES:
        task_type = "general"
    if _is_async_session(db):
        task = PgTask(
            note_id=note_id, prompt=prompt, status="pending",
            task_type=task_type, agent_id=agent_id,
        )
        db.add(task)
        await db.flush()
        await db.refresh(task)
        logger.info("Created task #%s (type=%s, note_id=%s)", task.id, task_type, note_id)
        return _task_to_dict_pg(task)

    cursor = await db.execute(
        "INSERT INTO tasks (note_id, task_type, prompt, agent_id) VALUES (?, ?, ?, ?)",
        (note_id, task_type, prompt, agent_id),
    )
    task_id = cursor.lastrowid
    await db.commit()
    logger.info("Created task #%s (type=%s, note_id=%s)", task_id, task_type, note_id)
    return await get_task(db, task_id)  # type: ignore[return-value]


# ── Read ────────────────────────────────────────────────────────────────────


async def get_task(db, task_id: int) -> dict | None:
    if _is_async_session(db):
        task = await db.get(PgTask, task_id)
        return _task_to_dict_pg(task) if task is not None else None

    cursor = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return _task_to_dict_sqlite(row)


async def list_tasks(
    db,
    status: str | None = None,
    note_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict], int]:
    if _is_async_session(db):
        where = []
        params: dict = {}
        if status:
            where.append(PgTask.status == status)
            params["status"] = status
        if note_id is not None:
            where.append(PgTask.note_id == note_id)
            params["note_id"] = note_id
        from sqlalchemy import and_ as sa_and
        stmt_count = select(func.count()).select_from(PgTask)
        stmt_rows = select(PgTask).order_by(PgTask.created_at.desc()).limit(limit).offset(offset)
        if where:
            stmt_count = stmt_count.where(sa_and(*where))
            stmt_rows = stmt_rows.where(sa_and(*where))
        total = (await db.execute(stmt_count)).scalar_one()
        rows = (await db.execute(stmt_rows)).scalars().all()
        return [_task_to_dict_pg(r) for r in rows], int(total or 0)

    clauses: list[str] = []
    params_list: list = []
    if status:
        clauses.append("status = ?")
        params_list.append(status)
    if note_id is not None:
        clauses.append("note_id = ?")
        params_list.append(note_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    count_cursor = await db.execute(f"SELECT COUNT(*) AS c FROM tasks {where}", params_list)
    total = (await count_cursor.fetchone())["c"]
    cursor = await db.execute(
        f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params_list, limit, offset),
    )
    rows = await cursor.fetchall()
    return [_task_to_dict_sqlite(r) for r in rows], total


# ── Complete / fail ─────────────────────────────────────────────────────────


async def complete_task(db, task_id: int, result: str) -> dict | None:
    """Mark ``task_id`` as completed and (best-effort) create a note from the result.

    Behavior is consistent across backends: the result is classified by the LLM
    and stored as a new note linked back to the task via ``source='task:<id>'``
    in the SQLite path; for PostgreSQL we use ``source_metadata`` to record the
    provenance (the ``notes.source`` column doesn't have a ``task:<id>`` enum
    value so we encode it in source_metadata).
    """
    from datetime import datetime, timezone
    task = await get_task(db, task_id)
    if task is None:
        return None

    is_pg = _is_async_session(db)
    now = datetime.now(timezone.utc)

    if is_pg:
        db_task = await db.get(PgTask, task_id)
        db_task.status = "completed"
        db_task.result = result
        db_task.completed_at = now
    else:
        await db.execute(
            "UPDATE tasks SET status = 'completed', result = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (result, task_id),
        )

    note_id = task.get("note_id")
    if note_id is not None:
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

            if is_pg:
                from datetime import date as _date
                new_note = PgNote(
                    title=f"Task #{task_id} result",
                    content=result,
                    para_category=para_category,
                    sub_category=sub_category,
                    priority=priority,
                    deadline=_date.fromisoformat(deadline) if deadline else None,
                    tags=tags,
                    source="task_result",
                    source_metadata={"task_id": task_id, "task_type": task.get("task_type")},
                    llm_model=llm_model,
                    llm_confidence=llm_confidence,
                    llm_reasoning=llm_reasoning,
                )
                db.add(new_note)
                await db.flush()
                new_note_id = new_note.id
                db.add(PgHistory(
                    note_id=new_note_id, action="created",
                    new_value=f"task:{task_id}",
                ))
                await db.flush()
            else:
                cursor = await db.execute(
                    """
                    INSERT INTO notes (title, content, para_category, sub_category, priority, deadline,
                                        tags, source, llm_model, llm_confidence, llm_reasoning)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"Task #{task_id} result", result, para_category, sub_category, priority, deadline,
                        json.dumps(tags, ensure_ascii=False), f"task:{task_id}",
                        llm_model, llm_confidence, llm_reasoning,
                    ),
                )
                new_note_id = cursor.lastrowid
                await db.execute(
                    "INSERT INTO history (note_id, action, new_value) VALUES (?, 'created', ?)",
                    (new_note_id, f"task:{task_id}"),
                )
            logger.info("Created note #%s from task #%s result", new_note_id, task_id)
        except Exception:
            logger.warning("Failed to create note from task #%s result", task_id, exc_info=True)

    if not is_pg:
        await db.commit()
    logger.info("Completed task #%s", task_id)
    return await get_task(db, task_id)


async def fail_task(db, task_id: int, reason: str) -> dict | None:
    from datetime import datetime, timezone
    task = await get_task(db, task_id)
    if task is None:
        return None
    now = datetime.now(timezone.utc)
    if _is_async_session(db):
        db_task = await db.get(PgTask, task_id)
        db_task.status = "failed"
        db_task.result = reason
        db_task.completed_at = now
    else:
        await db.execute(
            "UPDATE tasks SET status = 'failed', result = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (reason, task_id),
        )
        await db.commit()
    logger.info("Failed task #%s: %s", task_id, reason)
    return await get_task(db, task_id)


# ── Auto-extract from text ───────────────────────────────────────────────────


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
