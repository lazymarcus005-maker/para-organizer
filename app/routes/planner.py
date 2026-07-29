"""/api/plan, /api/activity-patterns — temporal reasoning + planner (SB-07)."""

from fastapi import APIRouter, Query

from app.planner import generate_plan, get_activity_patterns

router = APIRouter(prefix="/api", tags=["planner"])


@router.get("/plan")
async def get_plan(horizon: int = Query(default=7, ge=1, le=90)):
    return await generate_plan(horizon)


@router.get("/activity-patterns")
async def get_activity_patterns_endpoint(days: int = Query(default=30, ge=1, le=365)):
    return await get_activity_patterns(days)
