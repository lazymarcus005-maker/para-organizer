"""/api/import and /api/export/download — bulk JSON import and file downloads."""

import io
import json
import zipfile
from datetime import date

import aiosqlite
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse

from app.database import get_db
from app.models import PARA_CATEGORIES, PRIORITIES
from app.routes.export import _note_to_markdown, _safe_filename
from app.routes.notes import require_api_key
from app.utils import row_to_note

router = APIRouter(prefix="/api", tags=["import_export"])

MAX_IMPORT_SIZE = 10 * 1024 * 1024  # 10 MB


async def _fetch_all_notes(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute("SELECT * FROM notes ORDER BY para_category, created_at DESC")
    rows = await cursor.fetchall()
    return [row_to_note(r) for r in rows]


@router.post("/import", dependencies=[Depends(require_api_key)])
async def import_notes(file: UploadFile = File(...), db: aiosqlite.Connection = Depends(get_db)):
    # Strip any parameters (e.g. "application/json; charset=utf-8") before matching
    # so clients that annotate the charset aren't wrongly rejected.
    media_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if media_type not in ("application/json", "text/json", "text/plain", ""):
        raise HTTPException(status_code=400, detail="File must be JSON")

    raw = await file.read(MAX_IMPORT_SIZE + 1)
    if len(raw) > MAX_IMPORT_SIZE:
        raise HTTPException(status_code=413, detail="Import file too large (max 10 MB)")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON file")
    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail="JSON must be an array of note objects")

    imported = 0
    skipped = 0
    errors: list[dict] = []

    for index, item in enumerate(data):
        if not isinstance(item, dict):
            skipped += 1
            errors.append({"index": index, "error": "not an object"})
            continue

        title = item.get("title")
        content = item.get("content")
        if not isinstance(title, str) or not title.strip() or not isinstance(content, str) or not content.strip():
            skipped += 1
            errors.append({"index": index, "error": "title and content are required"})
            continue

        para_category = item.get("para_category", "inbox")
        if para_category not in PARA_CATEGORIES:
            para_category = "inbox"
        priority = item.get("priority", "medium")
        if priority not in PRIORITIES:
            priority = "medium"
        deadline = item.get("deadline")
        if deadline is not None:
            try:
                date.fromisoformat(str(deadline)[:10])
            except ValueError:
                deadline = None
        tags = item.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        tags = [t for t in tags if isinstance(t, str)]
        source = item.get("source", "manual")
        if not isinstance(source, str) or not source:
            source = "manual"

        await db.execute(
            """INSERT INTO notes (title, content, para_category, priority, deadline, tags, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                title.strip(), content, para_category, priority, deadline,
                json.dumps(tags, ensure_ascii=False), source,
            ),
        )
        imported += 1

    await db.commit()
    return {"imported": imported, "skipped": skipped, "errors": errors}


@router.get("/export/download")
async def export_download(
    format: str = Query(default="json", pattern="^(md|json)$"),
    db: aiosqlite.Connection = Depends(get_db),
):
    notes = await _fetch_all_notes(db)

    if format == "json":
        payload = json.dumps(notes, default=str, ensure_ascii=False, indent=2)
        return Response(
            content=payload,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=para-export.json"},
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for note in notes:
            zf.writestr(f"{note['para_category']}/{_safe_filename(note)}", _note_to_markdown(note))
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=para-export.zip"},
    )
