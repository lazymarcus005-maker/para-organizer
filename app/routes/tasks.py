"""/api/tasks/* — task delegation for Hermes agents (SB-02)."""

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from app.database import get_db
from app.models import TaskComplete, TaskCreate, TaskFail
from app.routes.notes import require_api_key
from app.tasks import complete_task, create_task, fail_task, get_task, list_tasks

logger = logging.getLogger("para.routes.tasks")
router = APIRouter(prefix="/api", tags=["tasks"])


@router.post("/tasks", dependencies=[Depends(require_api_key)])
async def create_task_endpoint(payload: TaskCreate, db: aiosqlite.Connection = Depends(get_db)):
    task = await create_task(db, payload.note_id, payload.prompt, payload.task_type, payload.agent_id)
    return task


@router.get("/tasks")
async def list_tasks_endpoint(
    status: str | None = Query(default=None),
    note_id: int | None = Query(default=None),
    limit: int = Query(default=20, le=200),
    offset: int = Query(default=0, ge=0),
    db: aiosqlite.Connection = Depends(get_db),
):
    tasks, total = await list_tasks(db, status=status, note_id=note_id, limit=limit, offset=offset)
    return {"tasks": tasks, "total": total, "limit": limit, "offset": offset}


@router.get("/tasks/{task_id}")
async def get_task_endpoint(task_id: int, db: aiosqlite.Connection = Depends(get_db)):
    task = await get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/tasks/{task_id}/complete", dependencies=[Depends(require_api_key)])
async def complete_task_endpoint(task_id: int, payload: TaskComplete, db: aiosqlite.Connection = Depends(get_db)):
    task = await complete_task(db, task_id, payload.result)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/tasks/{task_id}/fail", dependencies=[Depends(require_api_key)])
async def fail_task_endpoint(task_id: int, payload: TaskFail, db: aiosqlite.Connection = Depends(get_db)):
    task = await fail_task(db, task_id, payload.reason)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task
