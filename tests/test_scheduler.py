"""Tests for all five scheduler jobs."""

from datetime import date, datetime, timedelta

import pytest

from app.database import get_connection
from app.scheduler import (
    auto_archive_completed,
    build_digest,
    check_deadlines_and_notify,
    check_stale_projects,
    reclassify_low_confidence_notes,
    scheduler,
    send_weekly_digest,
)
from tests.conftest import insert_note


def test_scheduler_registers_exactly_five_jobs():
    assert {job.id for job in scheduler.get_jobs()} == {
        "reclassify", "auto_archive", "deadline_check", "stale_check", "weekly_digest"
    }


@pytest.mark.asyncio
async def test_reclassify_job(test_db, mock_llm):
    note_id = await insert_note(llm_confidence=0.2, para_category="inbox")
    assert await reclassify_low_confidence_notes() == 1
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT para_category, llm_confidence FROM notes WHERE id=?", (note_id,)
        )).fetchone()
    assert row["para_category"] == "projects"
    assert row["llm_confidence"] == 0.95


@pytest.mark.asyncio
async def test_auto_archive_job(test_db):
    now = datetime(2026, 7, 25, 2)
    old = (now - timedelta(days=31)).isoformat(sep=" ")
    note_id = await insert_note(status="completed", updated_at=old)
    assert await auto_archive_completed(now) == 1
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT status, para_category, archived_at FROM notes WHERE id=?", (note_id,)
        )).fetchone()
    assert row["status"] == "archived"
    assert row["para_category"] == "archives"
    assert row["archived_at"]


@pytest.mark.asyncio
async def test_deadline_job_sends_and_deduplicates(test_db, monkeypatch):
    today = date(2026, 7, 25)
    await insert_note(deadline=(today + timedelta(days=3)).isoformat())
    calls = []

    async def notify(note, days_left):
        calls.append((note["id"], days_left))
        return True

    monkeypatch.setattr("app.scheduler.notify_deadline", notify)
    assert await check_deadlines_and_notify(today) == 1
    assert await check_deadlines_and_notify(today) == 0
    assert calls == [(1, 3)]


@pytest.mark.asyncio
async def test_stale_project_job(test_db, monkeypatch):
    now = datetime(2026, 7, 25, 18)
    await insert_note(updated_at=(now - timedelta(days=15)).isoformat(sep=" "))
    await insert_note(title="Fresh", updated_at=now.isoformat(sep=" "))
    calls = []

    async def notify(note):
        calls.append(note["title"])
        return True

    monkeypatch.setattr("app.scheduler.notify_stale", notify)
    assert await check_stale_projects(now) == 1
    assert calls == ["Test note"]


@pytest.mark.asyncio
async def test_digest_build_and_send_job(test_db, monkeypatch):
    now = datetime(2026, 7, 25, 8)
    await insert_note(title="Active project", created_at=now.isoformat(sep=" "),
                      updated_at=now.isoformat(sep=" "))
    data = await build_digest(now)
    assert data["total_notes"] == 1
    assert data["new_notes_count"] == 1
    assert data["active_projects"][0]["title"] == "Active project"

    captured = []

    async def send(data):
        captured.append(data)
        return True

    monkeypatch.setattr("app.scheduler.send_digest", send)
    assert await send_weekly_digest(now) is True
    assert captured[0]["total_notes"] == 1
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT type, status FROM notifications WHERE type='digest'"
        )).fetchone()
    assert dict(row) == {"type": "digest", "status": "sent"}
