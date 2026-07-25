"""/api/export — export notes as markdown zip or JSON."""

import io
import json
import zipfile

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from app.database import get_db
from app.utils import row_to_note

router = APIRouter(prefix="/api", tags=["export"])


def _safe_filename(note: dict) -> str:
    title = "".join(c if c.isalnum() or c in " -_" else "_" for c in note["title"]).strip() or "untitled"
    return f"{note['id']:04d}_{title[:60]}.md"


def _note_to_markdown(note: dict) -> str:
    lines = [
        f"# {note['title']}",
        "",
        f"- Category: {note['para_category']}" + (f" / {note['sub_category']}" if note['sub_category'] else ""),
        f"- Status: {note['status']}",
        f"- Priority: {note['priority']}",
    ]
    if note.get("deadline"):
        lines.append(f"- Deadline: {note['deadline']}")
    if note.get("tags"):
        lines.append(f"- Tags: {', '.join(note['tags'])}")
    lines.append(f"- Source: {note['source']}")
    lines.append(f"- Created: {note['created_at']}")
    lines.append("")
    lines.append(note["content"])
    return "\n".join(lines)


@router.get("/export")
async def export_notes(
    format: str = Query(default="json", pattern="^(md|json)$"),
    db: aiosqlite.Connection = Depends(get_db),
):
    cursor = await db.execute("SELECT * FROM notes ORDER BY para_category, created_at DESC")
    rows = await cursor.fetchall()
    notes = [row_to_note(r) for r in rows]

    if format == "json":
        payload = json.dumps(notes, default=str, ensure_ascii=False, indent=2)
        return JSONResponse(content=json.loads(payload))

    if format == "md":
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

    raise HTTPException(status_code=422, detail="format must be 'md' or 'json'")
