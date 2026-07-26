"""Conversational chat mode tests: plain-text routing, history, RAG, /note distillation."""

import json

import pytest

from app.chat import get_history
from app.config import settings
from app.database import get_connection
from app.integrations.telegram_bot import handle_text
from tests.conftest import insert_note


@pytest.mark.asyncio
async def test_plain_text_routes_to_chat_not_note(test_db, mock_chat_llm):
    reply, markup = await handle_text("สวัสดีครับ", 123)
    assert reply == "สวัสดีค่ะ นี่คือคำตอบจำลองจากบอท"
    assert markup is None
    async with get_connection() as db:
        note_count = (await (await db.execute("SELECT COUNT(*) c FROM notes")).fetchone())["c"]
    assert note_count == 0


@pytest.mark.asyncio
async def test_history_persists_across_turns(test_db, mock_chat_llm):
    await handle_text("ข้อความแรก", 123)
    await handle_text("ข้อความที่สอง", 123)
    history = await get_history(123)
    assert [m["content"] for m in history] == [
        "ข้อความแรก",
        "สวัสดีค่ะ นี่คือคำตอบจำลองจากบอท",
        "ข้อความที่สอง",
        "สวัสดีค่ะ นี่คือคำตอบจำลองจากบอท",
    ]
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]


@pytest.mark.asyncio
async def test_history_trims_at_chat_history_max(test_db, mock_chat_llm, monkeypatch):
    monkeypatch.setattr(settings, "CHAT_HISTORY_MAX", 4)
    for i in range(5):
        await handle_text(f"ข้อความ {i}", 123)
    history = await get_history(123)
    assert len(history) == 4
    # Oldest turns trimmed away; the most recent user/assistant pair survives.
    assert history[-2]["content"] == "ข้อความ 4"
    assert history[-1]["content"] == "สวัสดีค่ะ นี่คือคำตอบจำลองจากบอท"


@pytest.mark.asyncio
async def test_reset_and_clear_wipe_history(test_db, mock_chat_llm):
    await handle_text("ข้อความแรก", 123)
    assert await get_history(123)

    reply, _ = await handle_text("/reset", 123)
    assert "ล้าง" in reply
    assert await get_history(123) == []

    await handle_text("ข้อความใหม่", 123)
    reply2, _ = await handle_text("/clear", 123)
    assert "ล้าง" in reply2
    assert await get_history(123) == []


@pytest.mark.asyncio
async def test_note_with_args_creates_directly_and_clears_history(test_db, mock_llm, mock_chat_llm):
    await handle_text("คุยเล่นก่อนนะ", 123)
    assert await get_history(123)

    reply, _ = await handle_text("/note นัดหมอฟัน", 123)
    assert "✅ บันทึกแล้ว!" in reply
    async with get_connection() as db:
        count = (await (await db.execute("SELECT COUNT(*) c FROM notes")).fetchone())["c"]
    assert count == 1
    assert await get_history(123) == []


@pytest.mark.asyncio
async def test_note_without_args_distills_from_conversation_then_clears(test_db, mock_llm, monkeypatch):
    import app.chat as chat

    async def fake_call_ollama(model, prompt=None, format="json", messages=None, **kwargs):
        if messages and messages[0]["content"] == chat.DISTILL_SYSTEM_PROMPT:
            return "นัดหมอฟัน\nต้องไปหาหมอฟันวันศุกร์นี้"
        return "สวัสดีค่ะ"

    monkeypatch.setattr(chat, "call_ollama", fake_call_ollama)

    await handle_text("ต้องไปหาหมอฟันวันศุกร์นี้", 123)
    reply, _ = await handle_text("/note", 123)

    assert "✅ บันทึกแล้ว!" in reply
    async with get_connection() as db:
        note = await (await db.execute("SELECT title, content FROM notes")).fetchone()
    assert note["title"] == "นัดหมอฟัน"
    assert "หาหมอฟัน" in note["content"]
    assert await get_history(123) == []


@pytest.mark.asyncio
async def test_note_without_args_and_no_history_returns_hint(test_db):
    reply, _ = await handle_text("/note", 123)
    assert "ยังไม่มีบทสนทนา" in reply
    async with get_connection() as db:
        count = (await (await db.execute("SELECT COUNT(*) c FROM notes")).fetchone())["c"]
    assert count == 0


@pytest.mark.asyncio
async def test_ask_command_behaves_like_chat(test_db, mock_chat_llm):
    reply, markup = await handle_text("/ask จะวางแผนงานนี้ยังไงดี", 123)
    assert reply == "สวัสดีค่ะ นี่คือคำตอบจำลองจากบอท"
    assert markup is None
    assert await get_history(123)


@pytest.mark.asyncio
async def test_rag_retrieval_injects_note_context(test_db, mock_chat_llm):
    await insert_note(title="Contabo server", content="server health monitoring notes")

    await handle_text("เล่าเรื่อง Contabo server ให้ฟังหน่อย", 123)

    assert mock_chat_llm
    prompt_text = json.dumps(mock_chat_llm[-1]["messages"], ensure_ascii=False)
    assert "Contabo server" in prompt_text
    assert "server health monitoring notes" in prompt_text
