"""/api graph reasoning endpoints (SB-05)."""

from fastapi import APIRouter, HTTPException, Query

from app.graph import (
    find_path,
    get_clusters,
    get_cross_category_links,
    get_full_graph,
    get_related,
    get_subgraph,
)

router = APIRouter(prefix="/api", tags=["graph"])


@router.get("/graph")
async def graph():
    return await get_full_graph()


@router.get("/notes/{note_id}/graph")
async def note_graph(note_id: int, depth: int = Query(default=2, ge=1, le=5)):
    return await get_subgraph(note_id, depth)


@router.get("/graph/path")
async def graph_path(
    from_id: int = Query(alias="from"),
    to_id: int = Query(alias="to"),
    max_depth: int = Query(default=5, ge=1, le=10),
):
    result = await find_path(from_id, to_id, max_depth)
    if result is None:
        raise HTTPException(status_code=404, detail="No path found")
    return result


@router.get("/graph/clusters")
async def graph_clusters(min_size: int = Query(default=2, ge=1)):
    return await get_clusters(min_size)


@router.get("/notes/{note_id}/related")
async def note_related(note_id: int, limit: int = Query(default=10, ge=1, le=50)):
    return await get_related(note_id, limit)


@router.get("/notes/{note_id}/cross-category")
async def note_cross_category(note_id: int):
    return await get_cross_category_links(note_id)
