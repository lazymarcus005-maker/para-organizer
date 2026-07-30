"""Web UI pages (Jinja2 + HTMX + Tailwind CDN)."""

from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import get_db
from app.models import PARA_CATEGORIES, NoteCreate
from app.routes.backup import list_backup_files
from app.routes.notes import create_note as api_create_note
from app.routes.para import para_tree as api_para_tree
from app.routes.search import search_notes as api_search_notes
from app.routes.settings import get_settings_dict
from app.routes.stats import get_stats as api_get_stats
from app.utils import row_to_note

router = APIRouter(tags=["pages"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

KANBAN_CATEGORIES = ["projects", "areas", "resources", "archives"]


@router.get("/")
async def index(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    tree = await api_para_tree(db)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"tree": tree["categories"], "columns": KANBAN_CATEGORIES, "api_key": settings.PARA_SECRET_KEY},
    )


@router.get("/board")
async def board_page(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    return templates.TemplateResponse(request, "board.html", {})


@router.get("/partials/board")
async def board_partial(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    tree = await api_para_tree(db)
    return templates.TemplateResponse(
        request, "_board.html", {"tree": tree["categories"], "columns": KANBAN_CATEGORIES}
    )


@router.get("/notes/{note_id}")
async def note_detail(request: Request, note_id: int, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
    row = await cursor.fetchone()
    note = row_to_note(row) if row else None
    return templates.TemplateResponse(
        request, "note_detail.html", {"note": note, "categories": PARA_CATEGORIES},
        status_code=200 if note else 404,
    )


@router.get("/new")
async def new_note_form(request: Request):
    return templates.TemplateResponse(request, "note_new.html", {})


@router.post("/new")
async def create_note_from_form(
    title: str = Form(...),
    content: str = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
):
    payload = NoteCreate(title=title, content=content, source="manual", auto_classify=True)
    note = await api_create_note(payload, db)
    return RedirectResponse(url=f"/notes/{note['id']}", status_code=303)


@router.get("/stats")
async def stats_page(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    stats = await api_get_stats(db)
    return templates.TemplateResponse(request, "stats.html", {"stats": stats})


@router.get("/search")
async def search_page(request: Request, q: str = Query(default=""), db: aiosqlite.Connection = Depends(get_db)):
    results = await api_search_notes(q=q, limit=20, db=db) if q else {"results": [], "total": 0}
    return templates.TemplateResponse(request, "search.html", {"query": q, "results": results["results"]})


@router.get("/settings")
async def settings_page(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    settings_values = await get_settings_dict(db)
    backups = list_backup_files()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"settings_values": settings_values, "backups": backups, "api_key": settings.PARA_SECRET_KEY},
    )


@router.get("/graph")
async def graph_page(request: Request):
    return templates.TemplateResponse(request, "graph.html", {})
