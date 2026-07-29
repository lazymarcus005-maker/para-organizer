"""Tests for SB-03 context injection: app.context.build_context, the
GET /api/context endpoint, and related_context in cron webhook responses."""

from datetime import date, datetime, timedelta

import pytest

from app.config import settings
from app.context import build_context
from app.database import get_connection
from tests.conftest import insert_note


@pytest.mark.asyncio
async def test_build_context_empty_db_has_all_keys(test_db):
    ctx = await build_context("anything")
    assert ctx["topic"] == "anything"
    assert ctx["related_notes"] == []
    assert ctx["upcoming_deadlines"] == []
    assert ctx["pending_tasks"] == []
    assert ctx["recent_activity"] == []
    assert ctx["graph_neighbors"] == []
    assert ctx["quick_stats"]["total_notes"] == 0
    assert ctx["quick_stats"]["inbox_count"] == 0
    assert ctx["generated_at"]


@pytest.mark.asyncio
async def test_build_context_related_notes_via_fts(test_db):
    note_id = await insert_note(title="Deploy pipeline", content="kubernetes deployment automation")
    ctx = await build_context("kubernetes")
    assert [n["id"] for n in ctx["related_notes"]] == [note_id]
    hit = ctx["related_notes"][0]
    assert hit["title"] == "Deploy pipeline"
    assert hit["para_category"] == "projects"
    assert "kubernetes" in hit["snippet"]
    assert hit["relevance"] > 0


@pytest.mark.asyncio
async def test_build_context_upcoming_deadlines(test_db):
    deadline = (date.today() + timedelta(days=5)).isoformat()
    note_id = await insert_note(title="Due soon", deadline=deadline)
    ctx = await build_context("nothing-matches-this-topic")
    assert [d["id"] for d in ctx["upcoming_deadlines"]] == [note_id]
    assert ctx["upcoming_deadlines"][0]["days_left"] == 5


@pytest.mark.asyncio
async def test_build_context_pending_tasks(test_db):
    note_id = await insert_note()
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO tasks (note_id, prompt, status) VALUES (?, ?, 'pending')",
            (note_id, "run the tests"),
        )
        await db.execute(
            "INSERT INTO tasks (note_id, prompt, status) VALUES (?, ?, 'dispatched')",
            (note_id, "deploy staging"),
        )
        await db.execute(
            "INSERT INTO tasks (note_id, prompt, status) VALUES (?, ?, 'completed')",
            (note_id, "already done"),
        )
        await db.commit()
    ctx = await build_context("x")
    prompts = {t["prompt"] for t in ctx["pending_tasks"]}
    assert prompts == {"run the tests", "deploy staging"}
    assert all(t["status"] in ("pending", "dispatched") for t in ctx["pending_tasks"])
    assert all("note_id" in t and "id" in t for t in ctx["pending_tasks"])


@pytest.mark.asyncio
async def test_build_context_recent_activity(test_db):
    fresh_id = await insert_note(
        title="Fresh", updated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    await insert_note(title="Old", updated_at="2020-01-01 10:00:00")
    ctx = await build_context("x")
    assert [a["id"] for a in ctx["recent_activity"]] == [fresh_id]
    assert ctx["recent_activity"][0]["title"] == "Fresh"


@pytest.mark.asyncio
async def test_build_context_graph_neighbors(test_db):
    source_id = await insert_note(title="Source", content="graph alpha topic")
    neighbor_id = await insert_note(title="Neighbor", content="unrelated body text")
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO links (from_note_id, to_note_id, link_type) VALUES (?, ?, 'references')",
            (source_id, neighbor_id),
        )
        await db.commit()
    ctx = await build_context("alpha")
    assert [n["id"] for n in ctx["graph_neighbors"]] == [neighbor_id]
    assert ctx["graph_neighbors"][0]["link_type"] == "references"


@pytest.mark.asyncio
async def test_build_context_quick_stats(test_db):
    await insert_note(para_category="projects")
    await insert_note(para_category="inbox")
    await insert_note(para_category="inbox")
    ctx = await build_context("x")
    stats = ctx["quick_stats"]
    assert stats["total_notes"] == 3
    assert stats["by_category"]["projects"] == 1
    assert stats["by_category"]["inbox"] == 2
    assert stats["inbox_count"] == 2


@pytest.mark.asyncio
async def test_context_endpoint_returns_package(client, test_db):
    await insert_note(title="Endpoint note", content="observable endpoint behavior")
    resp = client.get("/api/context", params={"topic": "endpoint", "limit": 3})
    assert resp.status_code == 200
    data = resp.json()
    assert data["topic"] == "endpoint"
    assert [n["title"] for n in data["related_notes"]] == ["Endpoint note"]
    assert "quick_stats" in data and "generated_at" in data


@pytest.mark.asyncio
async def test_cron_response_includes_related_context(client, test_db):
    auth = {"Authorization": f"Bearer {settings.PARA_SECRET_KEY}"}
    await insert_note(title="Server runbook", content="server maintenance schedule")
    resp = client.post(
        "/api/notes/cron",
        json={
            "title": "Cron server alert",
            "content": "server maintenance finished",
            "source": "cron:ops",
            "auto_classify": False,
        },
        headers=auth,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "related_context" in data
    assert len(data["related_context"]) <= 3
    titles = {n["title"] for n in data["related_context"]}
    assert "Server runbook" in titles


@pytest.mark.asyncio
async def test_cron_dedup_response_has_no_related_context(client, test_db):
    auth = {"Authorization": f"Bearer {settings.PARA_SECRET_KEY}"}
    payload = {
        "title": "Dedup ctx",
        "content": "duplicate context body",
        "source": "cron:ops",
        "auto_classify": False,
    }
    first = client.post("/api/notes/cron", json=payload, headers=auth)
    assert first.status_code == 200
    assert "related_context" in first.json()
    second = client.post("/api/notes/cron", json=payload, headers=auth)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert "related_context" not in second.json()
