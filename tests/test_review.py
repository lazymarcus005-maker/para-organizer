"""Weekly AI review tests (app.review + scheduler.send_weekly_review). The LLM
is mocked so we verify the analysis fed into the prompt and the delivery path."""

from datetime import datetime, timedelta

import httpx
import pytest

import app.review as review
import app.scheduler as scheduler
from app.database import get_connection
from app.review import gather_review_data, generate_weekly_review
from tests.conftest import insert_note

NOW = datetime(2026, 7, 25, 8)
OLD = (NOW - timedelta(days=20)).isoformat(sep=" ")
RECENT = NOW.isoformat(sep=" ")


@pytest.mark.asyncio
async def test_gather_detects_stale_neglected_and_completion(test_db):
    await insert_note(title="Stale P", para_category="projects", status="active",
                      created_at=OLD, updated_at=OLD)
    await insert_note(title="Active P", para_category="projects", status="active",
                      created_at=RECENT, updated_at=RECENT)
    await insert_note(title="Neglected Area", para_category="areas", status="active",
                      created_at=OLD, updated_at=OLD)
    await insert_note(title="Done", para_category="areas", status="completed",
                      created_at=OLD, updated_at=RECENT)

    data = await gather_review_data(NOW)

    stale_titles = [p["title"] for p in data["stale_projects"]]
    assert "Stale P" in stale_titles
    assert "Active P" not in stale_titles
    assert any(a["title"] == "Neglected Area" for a in data["neglected_areas"])
    # active=3 (2 projects + 1 area), completed=1 → 1/4.
    assert data["completion_rate"] == pytest.approx(0.25)
    assert len(data["completed_this_week"]) == 1


@pytest.mark.asyncio
async def test_generate_weekly_review_calls_llm_with_context(test_db, monkeypatch):
    await insert_note(title="Stale Project X", para_category="projects", status="active",
                      created_at=OLD, updated_at=OLD)
    calls = []

    async def fake_call(model, prompt=None, format="json", messages=None, task="chat", **kwargs):
        calls.append({"model": model, "messages": messages, "format": format, "task": task})
        return "## Weekly Review\n\nProgress noted.\n\n## 3 Action Recommendations\n1. a\n2. b\n3. c"

    monkeypatch.setattr(review, "call_ollama", fake_call)

    result = await generate_weekly_review(NOW)

    assert "3 Action Recommendations" in result
    assert calls, "LLM was not called"
    assert calls[0]["task"] == "review"
    assert calls[0]["format"] is None
    user_msg = calls[0]["messages"][-1]["content"]
    assert "Stale Project X" in user_msg
    assert "Stale projects" in user_msg
    assert "Completion rate" in user_msg


@pytest.mark.asyncio
async def test_generate_weekly_review_falls_back_on_llm_failure(test_db, monkeypatch):
    await insert_note(title="Stale Project Y", para_category="projects", status="active",
                      created_at=OLD, updated_at=OLD)

    async def boom(*args, **kwargs):
        raise httpx.HTTPError("provider down")

    monkeypatch.setattr(review, "call_ollama", boom)

    result = await generate_weekly_review(NOW)

    assert "3 Action Recommendations" in result
    assert "Stale Project Y" in result


@pytest.mark.asyncio
async def test_generate_weekly_review_falls_back_on_empty_response(test_db, monkeypatch):
    await insert_note(title="Some Project", para_category="projects", status="active",
                      created_at=OLD, updated_at=OLD)

    async def empty(*args, **kwargs):
        return "   "

    monkeypatch.setattr(review, "call_ollama", empty)

    result = await generate_weekly_review(NOW)
    assert "3 Action Recommendations" in result


@pytest.mark.asyncio
async def test_send_weekly_review_delivers_and_records_notification(test_db, monkeypatch):
    sent = []

    async def fake_generate(now=None):
        return "review text"

    async def fake_send(text):
        sent.append(text)
        return True

    monkeypatch.setattr(scheduler, "generate_weekly_review", fake_generate)
    monkeypatch.setattr(scheduler, "send_review", fake_send)

    assert await scheduler.send_weekly_review(NOW) is True
    assert sent == ["review text"]

    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT type, status FROM notifications WHERE type='review'"
        )).fetchone()
    assert dict(row) == {"type": "review", "status": "sent"}
