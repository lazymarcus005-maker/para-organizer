"""Web UI pages (Jinja2 + HTMX + Tailwind CDN)."""

import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database_v2 import get_db as get_pg_db
from app.health import compute_health
from app.models import PARA_CATEGORIES, NoteCreate
from app.models_v2 import Note
from app.routes.backup import list_backup_files
from app.routes.para import para_tree as api_para_tree
from app.routes_v2 import (
    _fetch_note,
    create_note as api_create_note,
    get_settings_dict,
    search_notes as api_search_notes,
)
from app.routes.stats import get_stats as api_get_stats

router = APIRouter(tags=["pages"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Cache-busting query param for /static assets so nginx's `expires 1h;
# Cache-Control: immutable` and mobile browser caches pick up new CSS/JS
# immediately after a deploy instead of serving stale assets for up to an hour.
templates.env.globals["static_version"] = str(int(time.time()))

KANBAN_CATEGORIES = ["projects", "areas", "resources", "archives"]


@router.get("/")
async def index(request: Request, session: AsyncSession = Depends(get_pg_db)):
    tree = await api_para_tree(session)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"tree": tree["categories"], "columns": KANBAN_CATEGORIES, "api_key": settings.PARA_SECRET_KEY},
    )


@router.get("/board")
async def board_page(request: Request):
    return templates.TemplateResponse(request, "board.html", {})


@router.get("/partials/board")
async def board_partial(request: Request, session: AsyncSession = Depends(get_pg_db)):
    tree = await api_para_tree(session)
    return templates.TemplateResponse(
        request, "_board.html", {"tree": tree["categories"], "columns": KANBAN_CATEGORIES}
    )


@router.get("/notes/{note_id}")
async def note_detail(request: Request, note_id: int, session: AsyncSession = Depends(get_pg_db)):
    note = None
    status_code = 404
    existing = await session.get(Note, note_id)
    if existing is not None:
        note = await _fetch_note(session, note_id)
        status_code = 200
    return templates.TemplateResponse(
        request, "note_detail.html", {"note": note, "categories": PARA_CATEGORIES},
        status_code=status_code,
    )


@router.get("/new")
async def new_note_form(request: Request):
    return templates.TemplateResponse(request, "note_new.html", {})


@router.post("/new")
async def create_note_from_form(
    title: str = Form(...),
    content: str = Form(...),
    session: AsyncSession = Depends(get_pg_db),
):
    payload = NoteCreate(title=title, content=content, source="manual", auto_classify=True)
    note = await api_create_note(payload, session)
    return RedirectResponse(url=f"/notes/{note['id']}", status_code=303)


@router.get("/stats")
async def stats_page(request: Request):
    stats = await api_get_stats()
    return templates.TemplateResponse(request, "stats.html", {"stats": stats})


@router.get("/search")
async def search_page(request: Request, q: str = Query(default=""), session: AsyncSession = Depends(get_pg_db)):
    results = await api_search_notes(q=q, limit=20, offset=0, session=session) if q else {"results": [], "total": 0}
    return templates.TemplateResponse(request, "search.html", {"query": q, "results": results["results"]})


@router.get("/settings")
async def settings_page(request: Request, session: AsyncSession = Depends(get_pg_db)):
    settings_values = await get_settings_dict(session)
    backups = list_backup_files()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"settings_values": settings_values, "backups": backups, "api_key": settings.PARA_SECRET_KEY},
    )


@router.get("/graph")
async def graph_page(request: Request):
    return templates.TemplateResponse(request, "graph.html", {})


@router.get("/health")
async def health_page(request: Request):
    data = await compute_health()
    return templates.TemplateResponse(request, "health.html", {"health": data})
