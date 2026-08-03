"""/api/para/* — PARA tree structure."""

import aiosqlite
from fastapi import APIRouter, Depends

from app.database import get_db
from app.models import PARA_CATEGORIES
from app.utils import row_to_note

router = APIRouter(prefix="/api/para", tags=["para"])


@router.get("/tree")
async def para_tree(db: aiosqlite.Connection = Depends(get_db)):
    from app.cache import get_cache
    cache = get_cache()
    cached = await cache.get("para:tree")
    if cached is not None:
        return cached
    tree = {}
    for category in PARA_CATEGORIES:
        cursor = await db.execute(
            "SELECT * FROM notes WHERE para_category = ? ORDER BY created_at DESC",
            (category,),
        )
        rows = await cursor.fetchall()
        notes = [row_to_note(r) for r in rows]
        tree[category] = {"count": len(notes), "notes": notes}

    result = {"categories": tree}
    await cache.set("para:tree", result, ttl=60)
    return result
