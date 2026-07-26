"""Telegram and cron webhook tests."""

import pytest

from app.database import get_connection
from app.integrations.telegram_bot import HELP_TEXT, handle_text, handle_update


@pytest.mark.asyncio
async def test_plain_text_routes_to_chat(test_db, mock_chat_llm):
    reply, markup = await handle_text("ต้องต่อทะเบียนรถ", 123, 99)
    assert reply == "สวัสดีค่ะ นี่คือคำตอบจำลองจากบอท"
    assert markup is None
    async with get_connection() as db:
        note_count = (await (await db.execute("SELECT COUNT(*) c FROM notes")).fetchone())["c"]
    assert note_count == 0


@pytest.mark.asyncio
async def test_note_command_creates_note(test_db, mock_llm):
    reply, _ = await handle_text("/note นัดหมอฟัน", 123)
    assert "Deadline" in reply
    async with get_connection() as db:
        count = (await (await db.execute("SELECT COUNT(*) c FROM notes")).fetchone())["c"]
    assert count == 1


@pytest.mark.asyncio
async def test_list_and_category_keyboard(test_db):
    from tests.conftest import insert_note
    await insert_note(title="Project one")
    reply, markup = await handle_text("/list projects", 123)
    assert "Project one" in reply
    assert markup.inline_keyboard[0][0].callback_data == "list:projects"


@pytest.mark.asyncio
async def test_search_command(test_db):
    from tests.conftest import insert_note
    await insert_note(title="Contabo", content="server health monitoring")
    reply, _ = await handle_text("/search server", 123)
    assert "Contabo" in reply


@pytest.mark.asyncio
async def test_deadlines_command(test_db):
    from datetime import date, timedelta
    from tests.conftest import insert_note
    await insert_note(title="Due soon", deadline=(date.today() + timedelta(days=3)).isoformat())
    reply, _ = await handle_text("/deadlines", 123)
    assert "Due soon" in reply


@pytest.mark.asyncio
async def test_done_command(test_db):
    from tests.conftest import insert_note
    note_id = await insert_note()
    reply, _ = await handle_text(f"/done {note_id}", 123)
    assert "Archive" in reply
    async with get_connection() as db:
        row = await (await db.execute("SELECT status, para_category FROM notes")).fetchone()
    assert dict(row) == {"status": "archived", "para_category": "archives"}


@pytest.mark.asyncio
async def test_move_command(test_db):
    from tests.conftest import insert_note
    note_id = await insert_note()
    reply, _ = await handle_text(f"/move {note_id} resources", 123)
    assert "Resources" in reply
    async with get_connection() as db:
        category = (await (await db.execute("SELECT para_category FROM notes")).fetchone())[0]
    assert category == "resources"


@pytest.mark.asyncio
async def test_stats_and_digest_commands(test_db):
    from tests.conftest import insert_note
    await insert_note()
    stats, _ = await handle_text("/stats", 123)
    digest, _ = await handle_text("/digest", 123)
    assert "Notes ทั้งหมด: 1" in stats
    assert "PARA Weekly Digest" in digest


@pytest.mark.asyncio
async def test_help_and_invalid_usage(test_db):
    assert (await handle_text("/help", 123))[0] == HELP_TEXT
    assert "ยังไม่มีบทสนทนา" in (await handle_text("/note", 123))[0]
    assert "ไม่รู้จัก" in (await handle_text("/wat", 123))[0]


@pytest.mark.asyncio
async def test_update_sends_reply_and_rejects_unknown_user(test_db, mock_chat_llm, sent_messages):
    update = {"message": {"message_id": 4, "text": "hello", "chat": {"id": 123}, "from": {"id": 123}}}
    await handle_update(update)
    assert len(sent_messages) == 1
    await handle_update({"message": {"text": "blocked", "chat": {"id": 999}, "from": {"id": 999}}})
    assert len(sent_messages) == 1


def test_webhook_auth_and_ack(client, mock_llm, sent_messages):
    update = {"message": {"text": "/help", "chat": {"id": 123}, "from": {"id": 123}}}
    assert client.post("/webhook/telegram", json=update).status_code == 401
    response = client.post(
        "/webhook/telegram", json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
    )
    assert response.json() == {"ok": True}
    assert sent_messages[0]["text"] == HELP_TEXT


def test_cron_webhook_auth_classification_and_tag_override(client, mock_llm):
    payload = {
        "content": "server report",
        "source": "cron:health",
        "auto_classify": True,
        "tags_override": ["server"],
    }
    assert client.post("/api/notes/cron", json=payload).status_code == 401
    response = client.post(
        "/api/notes/cron", json=payload,
        headers={"Authorization": "Bearer cron-secret"},
    )
    assert response.status_code == 200
    assert response.json()["source"] == "cron:health"
    assert response.json()["tags"] == ["server"]
    assert response.json()["para_category"] == "projects"


def test_cron_requires_cron_source(client):
    response = client.post(
        "/api/notes/cron",
        json={"content": "x", "source": "manual"},
        headers={"Authorization": "Bearer cron-secret"},
    )
    assert response.status_code == 422

