"""Tests for all scheduler jobs including auto-escalation."""

from datetime import date, datetime, timedelta

import pytest

from app.config import settings
from app.database import get_connection
from app.scheduler import (
    auto_archive_completed,
    auto_escalate_urgent_notes,
    build_digest,
    check_deadlines_and_notify,
    check_stale_projects,
    digest_trigger,
    reclassify_low_confidence_notes,
    reclassify_trigger,
    scheduler,
    send_weekly_digest,
)
from tests.conftest import insert_note


def test_scheduler_registers_expected_jobs():
    assert {job.id for job in scheduler.get_jobs()} == {
        "reclassify", "auto_archive", "escalate", "deadline_check", "stale_check",
        "weekly_digest", "weekly_review", "embed_backfill", "autonomous_tasks",
    }


def test_reclassify_trigger_falls_back_on_invalid_interval(monkeypatch):
    # A persisted 0/negative would make CronTrigger(\"*/0\") raise and brick startup.
    for bad in (0, -1):
        monkeypatch.setattr(settings, "RECLASSIFY_INTERVAL_HOURS", bad)
        assert "*/6" in str(reclassify_trigger())


def test_digest_trigger_falls_back_on_invalid_day(monkeypatch):
    monkeypatch.setattr(settings, "NOTIFY_DIGEST_DAY", "not-a-day")
    assert "mon" in str(digest_trigger())


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


@pytest.mark.asyncio
async def test_auto_escalate_notes_within_3_days(test_db, monkeypatch):
    """Test that low/medium priority notes within 3 days of deadline are escalated."""
    today = date(2026, 7, 25)
    # Note with deadline in 2 days (should be escalated)
    deadline_2d = (today + timedelta(days=2)).isoformat()
    note_id_1 = await insert_note(
        title="Urgent task", priority="low", deadline=deadline_2d
    )
    # Note with deadline in 3 days (should be escalated - at boundary)
    deadline_3d = (today + timedelta(days=3)).isoformat()
    note_id_2 = await insert_note(
        title="Medium task", priority="medium", deadline=deadline_3d
    )
    # Note with deadline in 4 days (should NOT be escalated)
    deadline_4d = (today + timedelta(days=4)).isoformat()
    note_id_3 = await insert_note(
        title="Not urgent", priority="low", deadline=deadline_4d
    )
    # Note with past deadline (should be skipped)
    deadline_past = (today - timedelta(days=1)).isoformat()
    note_id_4 = await insert_note(
        title="Past deadline", priority="low", deadline=deadline_past
    )

    calls = []

    async def notify(note, days_left, old_priority):
        calls.append((note["id"], days_left, old_priority))
        return True

    monkeypatch.setattr("app.scheduler.notify_escalation", notify)
    escalated = await auto_escalate_urgent_notes(today)
    
    # Should escalate 2 notes
    assert escalated == 2
    assert sorted([c[0] for c in calls]) == [note_id_1, note_id_2]
    
    # Verify priorities were updated
    async with get_connection() as db:
        row1 = await (await db.execute(
            "SELECT priority FROM notes WHERE id=?", (note_id_1,)
        )).fetchone()
        row2 = await (await db.execute(
            "SELECT priority FROM notes WHERE id=?", (note_id_2,)
        )).fetchone()
        row3 = await (await db.execute(
            "SELECT priority FROM notes WHERE id=?", (note_id_3,)
        )).fetchone()
        row4 = await (await db.execute(
            "SELECT priority FROM notes WHERE id=?", (note_id_4,)
        )).fetchone()
    
    assert row1["priority"] == "high"
    assert row2["priority"] == "high"
    assert row3["priority"] == "low"  # Not changed
    assert row4["priority"] == "low"  # Not changed


@pytest.mark.asyncio
async def test_auto_escalate_skips_high_priority_notes(test_db, monkeypatch):
    """Test that notes already at high priority are not escalated again."""
    today = date(2026, 7, 25)
    deadline_2d = (today + timedelta(days=2)).isoformat()
    
    # Already high priority
    note_id_high = await insert_note(
        title="Already high", priority="high", deadline=deadline_2d
    )
    # Low priority
    note_id_low = await insert_note(
        title="Low priority", priority="low", deadline=deadline_2d
    )

    calls = []

    async def notify(note, days_left, old_priority):
        calls.append(note["id"])
        return True

    monkeypatch.setattr("app.scheduler.notify_escalation", notify)
    escalated = await auto_escalate_urgent_notes(today)
    
    # Should only escalate low priority note
    assert escalated == 1
    assert calls == [note_id_low]


@pytest.mark.asyncio
async def test_auto_escalate_logs_history(test_db, monkeypatch):
    """Test that escalation is logged in history table."""
    today = date(2026, 7, 25)
    deadline_1d = (today + timedelta(days=1)).isoformat()
    note_id = await insert_note(
        title="Test escalation", priority="low", deadline=deadline_1d
    )

    async def notify(note, days_left, old_priority):
        return True

    monkeypatch.setattr("app.scheduler.notify_escalation", notify)
    await auto_escalate_urgent_notes(today)
    
    # Verify history entry was created
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT action, old_value, new_value, reason FROM history WHERE note_id=?",
            (note_id,),
        )).fetchone()
    
    assert row["action"] == "escalated"
    assert row["old_value"] == "low"
    assert row["new_value"] == "high"
    assert "deadline in 1 days" in row["reason"]


@pytest.mark.asyncio
async def test_auto_escalate_logs_notification(test_db, monkeypatch):
    """Test that escalation notification is logged."""
    today = date(2026, 7, 25)
    deadline_2d = (today + timedelta(days=2)).isoformat()
    note_id = await insert_note(
        title="Escalation test", priority="low", deadline=deadline_2d
    )

    async def notify(note, days_left, old_priority):
        return True

    monkeypatch.setattr("app.scheduler.notify_escalation", notify)
    await auto_escalate_urgent_notes(today)
    
    # Verify notification entry was created
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT type, status, payload FROM notifications WHERE note_id=?",
            (note_id,),
        )).fetchone()
    
    assert row["type"] == "escalation"
    assert row["status"] == "sent"
    assert "old_priority" in row["payload"]
    assert "days_left" in row["payload"]
