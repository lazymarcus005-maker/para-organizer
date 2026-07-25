"""Shared fixtures for Phase 3 integration tests."""

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_connection, init_db


@pytest_asyncio.fixture
async def test_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PARA_DB_PATH", str(tmp_path / "para-test.db"))
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USERS", "123")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "telegram-secret")
    monkeypatch.setattr(settings, "PARA_SECRET_KEY", "cron-secret")
    await init_db()
    yield settings.PARA_DB_PATH


@pytest.fixture
def mock_llm(monkeypatch):
    async def classify(title, content):
        return {
            "para_category": "projects",
            "sub_category": "ทดสอบ",
            "priority": "high",
            "deadline": "2026-08-15",
            "tags": ["ทดสอบ", "telegram"],
            "confidence": 0.95,
            "llm_model": "mock-model",
            "reasoning": "มีเป้าหมายชัดเจน",
        }

    import app.integrations.telegram_bot as telegram_bot
    import app.routes.cron_webhook as cron_webhook
    import app.scheduler as scheduler

    monkeypatch.setattr(telegram_bot, "classify_note", classify)
    monkeypatch.setattr(cron_webhook, "classify_note", classify)
    monkeypatch.setattr(scheduler, "classify_note", classify)
    return classify


@pytest.fixture
def sent_messages(monkeypatch):
    messages = []

    async def send(chat_id, text, reply_markup=None, retries=3):
        messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return True

    import app.integrations.telegram_bot as telegram_bot
    monkeypatch.setattr(telegram_bot, "send_telegram", send)
    return messages


@pytest.fixture
def client(test_db, monkeypatch):
    from app.main import app
    from app.scheduler import scheduler

    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "shutdown", lambda wait=False: None)
    with TestClient(app) as test_client:
        yield test_client


async def insert_note(**overrides):
    values = {
        "title": "Test note",
        "content": "Test content",
        "para_category": "projects",
        "status": "active",
        "priority": "medium",
        "deadline": None,
        "tags": "[]",
        "source": "manual",
        "llm_confidence": 0.9,
        "created_at": "2026-07-01 10:00:00",
        "updated_at": "2026-07-01 10:00:00",
    }
    values.update(overrides)
    async with get_connection() as db:
        cursor = await db.execute(
            """INSERT INTO notes
               (title, content, para_category, status, priority, deadline, tags,
                source, llm_confidence, created_at, updated_at)
               VALUES (:title, :content, :para_category, :status, :priority,
                       :deadline, :tags, :source, :llm_confidence, :created_at,
                       :updated_at)""",
            values,
        )
        await db.commit()
        return cursor.lastrowid

