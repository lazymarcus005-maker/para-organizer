"""Shared fixtures for Phase 3 integration tests."""

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.config import Settings, settings
from app.database import get_connection, init_db
from app.routes.settings import SETTINGS_KEYS

# The unmodified pydantic defaults (env/.env only, no DB-persisted overrides) —
# used to reset settings.* between tests regardless of what a local data/para.db
# happens to have persisted (see app.config._load_persisted_overrides).
_DEFAULT_SETTINGS = Settings()


@pytest_asyncio.fixture
async def test_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PARA_DB_PATH", str(tmp_path / "para-test.db"))
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USERS", "123")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "telegram-secret")
    monkeypatch.setattr(settings, "PARA_SECRET_KEY", "cron-secret")
    # PUT /api/settings mutates these live (see app.routes.settings.update_settings),
    # so a test that changes one would otherwise leak it into later tests via the
    # shared `settings` singleton. Force each to the pydantic default via monkeypatch
    # so it's automatically restored at teardown.
    for key in SETTINGS_KEYS:
        monkeypatch.setattr(settings, key, getattr(_DEFAULT_SETTINGS, key))
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
def mock_chat_llm(monkeypatch):
    calls = []

    async def fake_call_ollama(model, prompt=None, format="json", messages=None, **kwargs):
        calls.append({"model": model, "prompt": prompt, "format": format, "messages": messages})
        return "สวัสดีค่ะ นี่คือคำตอบจำลองจากบอท"

    import app.chat as chat
    monkeypatch.setattr(chat, "call_ollama", fake_call_ollama)
    return calls


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

