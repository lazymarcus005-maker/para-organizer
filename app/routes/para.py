"""/api/para/* — PARA tree structure."""

import json

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database_v2 import get_db as get_pg_db
from app.models import PARA_CATEGORIES
from app.models_v2 import Note

router = APIRouter(prefix="/api/para", tags=["para"])


def _note_to_dict(note: Note) -> dict:
    tags = note.tags if isinstance(note.tags, list) else json.loads(note.tags) if isinstance(note.tags, str) else []
    confidence = float(note.llm_confidence or 0.0)
    return {
        "id": note.id,
        "title": note.title,
        "content": note.content,
        "para_category": note.para_category,
        "sub_category": note.sub_category,
        "status": note.status,
        "priority": note.priority,
        "deadline": note.deadline.isoformat() if note.deadline else None,
        "tags": tags,
        "source": note.source,
        "llm_model": note.llm_model,
        "llm_confidence": confidence,
        "llm_reasoning": note.llm_reasoning,
        "created_at": note.created_at.isoformat() if note.created_at else None,
        "updated_at": note.updated_at.isoformat() if note.updated_at else None,
        "review_needed": note.llm_model is not None and confidence < settings.RECLASSIFY_CONFIDENCE_THRESHOLD,
    }


@router.get("/tree")
async def para_tree(session: AsyncSession = Depends(get_pg_db)):
    from app.cache import get_cache
    cache = get_cache()
    cached = await cache.get("para:tree")
    if cached is not None:
        return cached

    tree = {}
    for category in PARA_CATEGORIES:
        rows = (await session.execute(
            select(Note).where(Note.para_category == category).order_by(Note.created_at.desc())
        )).scalars().all()
        notes = [_note_to_dict(r) for r in rows]
        tree[category] = {"count": len(notes), "notes": notes}

    result = {"categories": tree}
    await cache.set("para:tree", result, ttl=60)
    return result
