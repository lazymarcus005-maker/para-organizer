"""Multimodal input: URL and text extraction (SB-12)."""

import json
import logging
import re
from html.parser import HTMLParser

import httpx

from app.classifier import classify_note
from app.database import get_connection
from app.utils import row_to_note

logger = logging.getLogger("para.multimodal")


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._pieces: list[str] = []
        self._in_title = False
        self._skip = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self._skip:
            self._pieces.append(data)

    def get_text(self) -> str:
        return " ".join(self._pieces)


async def extract_from_url(url: str) -> dict:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 PARA-Organizer"})
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        logger.warning("Failed to fetch URL %s", url, exc_info=True)
        return {"error": str(exc)}

    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
    except Exception:
        logger.warning("Failed to parse HTML from %s", url, exc_info=True)

    title = parser.title.strip() or url
    text = re.sub(r"\s+", " ", parser.get_text()).strip()
    return {
        "title": title,
        "content": text[:2000],
        "source_url": url,
        "content_type": "url",
    }


async def create_note_from_url(url: str, auto_classify: bool = True) -> dict:
    extracted = await extract_from_url(url)
    if "error" in extracted:
        return extracted

    title = extracted["title"] or url
    content = extracted["content"]

    para_category = "inbox"
    sub_category = None
    priority = "medium"
    deadline = None
    tags: list = []
    llm_model = None
    llm_confidence = 0.0
    llm_reasoning = None

    if auto_classify:
        result = await classify_note(title, content)
        para_category = result.get("para_category", "inbox")
        sub_category = result.get("sub_category")
        priority = result.get("priority", "medium")
        deadline = result.get("deadline")
        tags = result.get("tags", [])
        llm_model = result.get("llm_model")
        llm_confidence = float(result.get("confidence", 0.0))
        llm_reasoning = result.get("reasoning")

    async with get_connection() as db:
        cursor = await db.execute(
            """
            INSERT INTO notes (title, content, para_category, sub_category, priority, deadline,
                                tags, source, source_metadata, llm_model, llm_confidence, llm_reasoning)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title, content, para_category, sub_category, priority, deadline,
                json.dumps(tags, ensure_ascii=False), "url",
                json.dumps({"source_url": url}, ensure_ascii=False),
                llm_model, llm_confidence, llm_reasoning,
            ),
        )
        note_id = cursor.lastrowid
        await db.execute(
            "INSERT INTO history (note_id, action, old_value, new_value, reason) VALUES (?, ?, ?, ?, ?)",
            (note_id, "created", None, "url", None),
        )
        if auto_classify:
            await db.execute(
                "INSERT INTO history (note_id, action, old_value, new_value, reason) VALUES (?, ?, ?, ?, ?)",
                (note_id, "classified", None, para_category, llm_reasoning),
            )
        await db.commit()

        cursor = await db.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
        row = await cursor.fetchone()
        return row_to_note(row)


def extract_text_from_content(content: str, content_type: str) -> str:
    if content_type == "url":
        return content
    if content_type == "voice":
        return content
    return content
