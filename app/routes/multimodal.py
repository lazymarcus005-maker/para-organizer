"""/api/notes/from-url — multimodal capture from a URL (SB-12)."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.multimodal import create_note_from_url
from app.routes.notes import require_api_key

router = APIRouter(prefix="/api", tags=["multimodal"])


class UrlNoteCreate(BaseModel):
    url: str = Field(min_length=1)
    auto_classify: bool = True


@router.post("/notes/from-url", dependencies=[Depends(require_api_key)])
async def create_note_from_url_route(payload: UrlNoteCreate):
    return await create_note_from_url(payload.url, payload.auto_classify)
