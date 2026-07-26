"""Integration tests for /api/notes/cron endpoint with dedup verification."""

import json

import pytest

from app.config import settings
from app.database import get_connection
from app.models import CronNoteCreate


@pytest.mark.asyncio
async def test_cron_requires_auth(client):
    """Cron endpoint requires Bearer token authentication."""
    response = client.post(
        "/api/notes/cron",
        json={
            "title": "Test",
            "content": "Test content",
            "source": "cron:test",
            "auto_classify": False,
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_cron_requires_bearer_token(client):
    """Cron endpoint rejects invalid auth tokens."""
    response = client.post(
        "/api/notes/cron",
        json={
            "title": "Test",
            "content": "Test content",
            "source": "cron:test",
            "auto_classify": False,
        },
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_cron_requires_cron_source(client):
    """Cron endpoint only accepts source starting with 'cron:'."""
    response = client.post(
        "/api/notes/cron",
        json={
            "title": "Test",
            "content": "Test content",
            "source": "telegram",
            "auto_classify": False,
        },
        headers={"Authorization": f"Bearer {settings.PARA_SECRET_KEY}"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_cron_creates_note_with_valid_auth(client, mock_llm):
    """Cron endpoint creates note with valid auth and cron: source."""
    response = client.post(
        "/api/notes/cron",
        json={
            "title": "Cron test note",
            "content": "This is a cron job note",
            "source": "cron:daily_check",
            "auto_classify": True,
        },
        headers={"Authorization": f"Bearer {settings.PARA_SECRET_KEY}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Cron test note"
    assert data["content"] == "This is a cron job note"
    assert data["source"] == "cron:daily_check"


@pytest.mark.asyncio
async def test_cron_dedup_same_payload_returns_existing(client, mock_llm):
    """Sending identical payload twice returns the existing note without creating duplicate."""
    payload = {
        "title": "Duplicate test",
        "content": "Identical content for dedup",
        "source": "cron:daily_check",
        "auto_classify": True,
    }
    
    # First request creates note
    response1 = client.post(
        "/api/notes/cron",
        json=payload,
        headers={"Authorization": f"Bearer {settings.PARA_SECRET_KEY}"},
    )
    assert response1.status_code == 200
    note_id_1 = response1.json()["id"]
    
    # Second identical request should return same note (dedup)
    response2 = client.post(
        "/api/notes/cron",
        json=payload,
        headers={"Authorization": f"Bearer {settings.PARA_SECRET_KEY}"},
    )
    assert response2.status_code == 200
    note_id_2 = response2.json()["id"]
    
    # Should be same note ID
    assert note_id_1 == note_id_2
    
    # Verify only one note was created in DB
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT COUNT(*) as cnt FROM notes WHERE source='cron:daily_check'"
        )).fetchone()
    assert row is not None and row["cnt"] == 1


@pytest.mark.asyncio
async def test_cron_dedup_different_content_creates_new(client, mock_llm):
    """Different content creates new note even with same source."""
    headers = {"Authorization": f"Bearer {settings.PARA_SECRET_KEY}"}
    
    # First note
    response1 = client.post(
        "/api/notes/cron",
        json={
            "title": "Test 1",
            "content": "Content A",
            "source": "cron:daily_check",
            "auto_classify": True,
        },
        headers=headers,
    )
    assert response1.status_code == 200
    note_id_1 = response1.json()["id"]
    
    # Second note with different content
    response2 = client.post(
        "/api/notes/cron",
        json={
            "title": "Test 1",
            "content": "Content B",  # Different
            "source": "cron:daily_check",
            "auto_classify": True,
        },
        headers=headers,
    )
    assert response2.status_code == 200
    note_id_2 = response2.json()["id"]
    
    # Should create separate notes
    assert note_id_1 != note_id_2
    
    # Verify two notes in DB
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT COUNT(*) as cnt FROM notes WHERE source='cron:daily_check'"
        )).fetchone()
    assert row is not None and row["cnt"] == 2


@pytest.mark.asyncio
async def test_cron_dedup_different_source_creates_new(client, mock_llm):
    """Same content with different source creates new note."""
    headers = {"Authorization": f"Bearer {settings.PARA_SECRET_KEY}"}
    content = "Identical content"
    
    # First note from source1
    response1 = client.post(
        "/api/notes/cron",
        json={
            "title": "Test",
            "content": content,
            "source": "cron:source1",
            "auto_classify": True,
        },
        headers=headers,
    )
    assert response1.status_code == 200
    note_id_1 = response1.json()["id"]
    
    # Second note from source2 with same content
    response2 = client.post(
        "/api/notes/cron",
        json={
            "title": "Test",
            "content": content,
            "source": "cron:source2",
            "auto_classify": True,
        },
        headers=headers,
    )
    assert response2.status_code == 200
    note_id_2 = response2.json()["id"]
    
    # Should create separate notes since source is different
    assert note_id_1 != note_id_2


@pytest.mark.asyncio
async def test_cron_respects_tags_override(client, mock_llm):
    """Cron endpoint respects tags_override when provided."""
    response = client.post(
        "/api/notes/cron",
        json={
            "title": "Test",
            "content": "Content",
            "source": "cron:test",
            "auto_classify": True,
            "tags_override": ["manual", "cron", "test"],
        },
        headers={"Authorization": f"Bearer {settings.PARA_SECRET_KEY}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["tags"] == ["manual", "cron", "test"]


@pytest.mark.asyncio
async def test_cron_auto_classify_false_skips_classification(client):
    """When auto_classify=False, note is created without LLM classification."""
    response = client.post(
        "/api/notes/cron",
        json={
            "title": "Unclassified",
            "content": "Raw content",
            "source": "cron:test",
            "auto_classify": False,
        },
        headers={"Authorization": f"Bearer {settings.PARA_SECRET_KEY}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["para_category"] == "inbox"  # Default category
    assert data["priority"] == "medium"  # Default priority
    assert data["llm_confidence"] == 0.0  # No LLM used


@pytest.mark.asyncio
async def test_cron_history_created_on_new_note(client, mock_llm):
    """Creating a new cron note logs it in history table."""
    response = client.post(
        "/api/notes/cron",
        json={
            "title": "History test",
            "content": "Test history logging",
            "source": "cron:history_test",
            "auto_classify": True,
        },
        headers={"Authorization": f"Bearer {settings.PARA_SECRET_KEY}"},
    )
    assert response.status_code == 200
    note_id = response.json()["id"]
    
    # Verify history entry
    async with get_connection() as db:
        history = await (await db.execute(
            "SELECT action, new_value FROM history WHERE note_id=? AND action='created'",
            (note_id,),
        )).fetchone()
    
    assert history is not None
    assert history["action"] == "created"
    assert history["new_value"] == "cron:history_test"


@pytest.mark.asyncio
async def test_cron_title_defaults_to_source(client, mock_llm):
    """When title is None, it defaults to source."""
    response = client.post(
        "/api/notes/cron",
        json={
            "content": "Content without title",
            "source": "cron:notitle",
            "auto_classify": False,
        },
        headers={"Authorization": f"Bearer {settings.PARA_SECRET_KEY}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "cron:notitle"


@pytest.mark.asyncio
async def test_cron_payload_validation_requires_content(client):
    """Cron endpoint requires content field."""
    response = client.post(
        "/api/notes/cron",
        json={
            "title": "No content",
            "source": "cron:test",
        },
        headers={"Authorization": f"Bearer {settings.PARA_SECRET_KEY}"},
    )
    assert response.status_code == 422  # Validation error


@pytest.mark.asyncio
async def test_cron_payload_validation_requires_source(client):
    """Cron endpoint requires source field."""
    response = client.post(
        "/api/notes/cron",
        json={
            "title": "No source",
            "content": "Content",
        },
        headers={"Authorization": f"Bearer {settings.PARA_SECRET_KEY}"},
    )
    assert response.status_code == 422  # Validation error
