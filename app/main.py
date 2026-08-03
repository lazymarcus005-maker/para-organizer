"""FastAPI app entrypoint: routers, startup migrations, static files."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.cache import get_cache
from app.database import init_db
from app.routes import (
    agents,
    backup,
    cron_webhook,
    events,
    export,
    feedback,
    graph,
    health,
    import_export,
    items,
    multimodal,
    notes,
    pages,
    para,
    planner,
    search,
    settings,
    stats,
    tasks,
    telegram_webhook,
)

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Initialise cache singleton on startup
    _ = get_cache()
    try:
        yield
    finally:
        pass


app = FastAPI(title="PARA Organizer", version="5.0.0", lifespan=lifespan)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(notes.router)
app.include_router(para.router)
app.include_router(search.router)
app.include_router(search.context_router)
app.include_router(stats.router)
app.include_router(export.router)
app.include_router(backup.router)
app.include_router(import_export.router)
app.include_router(settings.router)
app.include_router(telegram_webhook.router)
app.include_router(cron_webhook.router)
app.include_router(tasks.router)
app.include_router(items.router)
app.include_router(events.router)
app.include_router(pages.router)
app.include_router(planner.router)
app.include_router(feedback.router)
app.include_router(graph.router)
app.include_router(health.router)
app.include_router(multimodal.router)
app.include_router(agents.router)
