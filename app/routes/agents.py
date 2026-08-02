"""/api/agents/*, /api/shared, /api/conflicts — multi-agent memory (SB-10)."""

import logging

import aiosqlite
from fastapi import APIRouter, Depends, Query

from app.agents import detect_conflicts, get_agent_summary, get_shared_notes
from app.database import get_connection, get_db
from app.utils import row_to_note

logger = logging.getLogger("para.routes.agents")
router = APIRouter(prefix="/api", tags=["agents"])


@router.get("/agents")
async def list_agents():
    async with get_connection() as db:
        cursor = await db.execute(
            """SELECT DISTINCT agent_id FROM notes WHERE agent_id IS NOT NULL
               UNION
               SELECT DISTINCT agent_id FROM tasks WHERE agent_id IS NOT NULL"""
        )
        agent_ids = [row["agent_id"] for row in await cursor.fetchall()]

    agents = []
    for agent_id in agent_ids:
        summary = await get_agent_summary(agent_id)
        agents.append({
            "agent_id": agent_id,
            "notes_count": summary["notes_count"],
            "tasks_count": summary["tasks_count"],
            "last_active": summary["last_active"],
        })

    return {"agents": agents}


@router.get("/agents/{agent_id}/notes")
async def agent_notes(
    agent_id: str,
    limit: int = Query(default=20, le=200),
    db: aiosqlite.Connection = Depends(get_db),
):
    cursor = await db.execute(
        "SELECT * FROM notes WHERE agent_id = ? ORDER BY updated_at DESC LIMIT ?",
        (agent_id, limit),
    )
    rows = await cursor.fetchall()
    return {"notes": [row_to_note(r) for r in rows]}


@router.get("/agents/{agent_id}/tasks")
async def agent_tasks(
    agent_id: str,
    limit: int = Query(default=20, le=200),
    db: aiosqlite.Connection = Depends(get_db),
):
    cursor = await db.execute(
        "SELECT * FROM tasks WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
        (agent_id, limit),
    )
    rows = await cursor.fetchall()
    return {"tasks": [dict(r) for r in rows]}


@router.get("/agents/{agent_id}/activity")
async def agent_activity(
    agent_id: str,
    limit: int = Query(default=50, le=200),
    db: aiosqlite.Connection = Depends(get_db),
):
    cursor = await db.execute(
        """SELECT 'note' AS type, id, title AS title_or_prompt,
                  updated_at AS timestamp, status
           FROM notes WHERE agent_id = ?
           UNION ALL
           SELECT 'task' AS type, id, prompt AS title_or_prompt,
                  COALESCE(completed_at, created_at) AS timestamp, status
           FROM tasks WHERE agent_id = ?
           ORDER BY timestamp DESC LIMIT ?""",
        (agent_id, agent_id, limit),
    )
    rows = await cursor.fetchall()
    activity = [
        {
            "type": r["type"],
            "id": r["id"],
            "title_or_prompt": r["title_or_prompt"],
            "timestamp": r["timestamp"],
            "status": r["status"],
        }
        for r in rows
    ]
    return {"activity": activity}


@router.get("/shared/notes")
async def shared_notes(limit: int = Query(default=20, le=200)):
    notes = await get_shared_notes(limit)
    return {"notes": notes}


@router.get("/conflicts")
async def conflicts(days: int = Query(default=7, ge=1, le=365)):
    result = await detect_conflicts(days)
    return {"conflicts": result}
