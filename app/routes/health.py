"""/api/health — brain health dashboard (SB-11)."""

from fastapi import APIRouter

from app.health import compute_health

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health():
    return await compute_health()
