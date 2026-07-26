"""F0-2: LLM usage tracking (app.usage) + auto-logging in call_ollama + endpoint."""

import httpx
import pytest

import app.classifier as classifier
from app.database import get_connection
from app.usage import log_usage, usage_summary


# ─── log_usage ───

@pytest.mark.asyncio
async def test_log_usage_inserts_row(test_db):
    await log_usage(
        "deepseek-v4-flash", "classify",
        {"prompt_tokens": 10, "completion_tokens": 5}, note_id=None,
    )
    async with get_connection() as db:
        rows = await (await db.execute(
            "SELECT model, task, prompt_tokens, completion_tokens, note_id FROM llm_usage"
        )).fetchall()
    assert len(rows) == 1
    assert rows[0]["model"] == "deepseek-v4-flash"
    assert rows[0]["task"] == "classify"
    assert rows[0]["prompt_tokens"] == 10
    assert rows[0]["completion_tokens"] == 5
    assert rows[0]["note_id"] is None


@pytest.mark.asyncio
async def test_log_usage_tolerates_missing_and_none_usage(test_db):
    # Must not raise on partial/None usage; stores NULLs for absent counts.
    await log_usage("m", "chat", {})
    await log_usage("m", "chat", None)
    async with get_connection() as db:
        rows = await (await db.execute(
            "SELECT prompt_tokens, completion_tokens FROM llm_usage"
        )).fetchall()
    assert len(rows) == 2
    assert all(r["prompt_tokens"] is None and r["completion_tokens"] is None for r in rows)


@pytest.mark.asyncio
async def test_log_usage_never_raises_on_db_failure(monkeypatch):
    """Logging is best-effort: a broken DB connection must be swallowed."""
    from app import usage

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(usage, "get_connection", boom)
    # Should return without raising.
    await log_usage("m", "chat", {"prompt_tokens": 1})


# ─── usage_summary ───

@pytest.mark.asyncio
async def test_usage_summary_aggregates(test_db):
    await log_usage("model-a", "classify", {"prompt_tokens": 10, "completion_tokens": 2})
    await log_usage("model-a", "chat", {"prompt_tokens": 20, "completion_tokens": 4})
    await log_usage("model-b", "chat", {"prompt_tokens": 5, "completion_tokens": 1})

    summary = await usage_summary(7)

    assert summary["total_prompt_tokens"] == 35
    assert summary["total_completion_tokens"] == 7
    assert summary["total_calls"] == 3

    assert summary["by_model"]["model-a"]["prompt_tokens"] == 30
    assert summary["by_model"]["model-a"]["calls"] == 2
    assert summary["by_model"]["model-b"]["prompt_tokens"] == 5

    assert summary["by_task"]["chat"]["prompt_tokens"] == 25
    assert summary["by_task"]["chat"]["completion_tokens"] == 5
    assert summary["by_task"]["classify"]["calls"] == 1

    assert len(summary["by_day"]) == 1
    assert summary["by_day"][0]["calls"] == 3


@pytest.mark.asyncio
async def test_usage_summary_matches_manual_query(test_db):
    await log_usage("m", "chat", {"prompt_tokens": 3, "completion_tokens": 1})
    await log_usage("m", "review", {"prompt_tokens": 7, "completion_tokens": 2})

    summary = await usage_summary(7)
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT SUM(prompt_tokens) p, SUM(completion_tokens) c, COUNT(*) n FROM llm_usage"
        )).fetchone()
    assert summary["total_prompt_tokens"] == row["p"]
    assert summary["total_completion_tokens"] == row["c"]
    assert summary["total_calls"] == row["n"]


@pytest.mark.asyncio
async def test_usage_summary_respects_day_window(test_db):
    """Rows older than the window are excluded."""
    await log_usage("m", "chat", {"prompt_tokens": 100, "completion_tokens": 10})
    async with get_connection() as db:
        # Backdate the row well outside a 7-day window.
        await db.execute(
            "UPDATE llm_usage SET ts = datetime('now', '-30 days')"
        )
        await db.commit()

    summary = await usage_summary(7)
    assert summary["total_prompt_tokens"] == 0
    assert summary["total_calls"] == 0
    assert summary["by_model"] == {}


@pytest.mark.asyncio
async def test_usage_summary_empty_returns_structure(test_db):
    summary = await usage_summary(7)
    assert summary["total_prompt_tokens"] == 0
    assert summary["total_completion_tokens"] == 0
    assert summary["by_model"] == {}
    assert summary["by_task"] == {}
    assert summary["by_day"] == []


# ─── auto-logging hook in call_ollama ───

class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


@pytest.mark.asyncio
async def test_call_ollama_auto_logs_usage(test_db, monkeypatch):
    body = {
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }

    async def fake_post(self, url, json=None, headers=None):
        return _FakeResponse(body)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    out = await classifier.call_ollama("test-model", prompt="hi", task="classify")
    assert out == "hello"

    summary = await usage_summary(7)
    assert summary["total_prompt_tokens"] == 7
    assert summary["by_model"]["test-model"]["completion_tokens"] == 3
    assert summary["by_task"]["classify"]["calls"] == 1


@pytest.mark.asyncio
async def test_call_ollama_without_usage_block_still_returns(test_db, monkeypatch):
    body = {"choices": [{"message": {"content": "hi"}}]}  # no usage key

    async def fake_post(self, url, json=None, headers=None):
        return _FakeResponse(body)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    out = await classifier.call_ollama("m", prompt="x", task="chat")
    assert out == "hi"
    summary = await usage_summary(7)
    assert summary["total_calls"] == 1
    assert summary["total_prompt_tokens"] == 0


# ─── endpoint ───

def test_usage_endpoint_returns_structure(client):
    resp = client.get("/api/usage?days=7")
    assert resp.status_code == 200
    data = resp.json()
    for key in ("total_prompt_tokens", "total_completion_tokens", "total_calls",
                "by_model", "by_task", "by_day"):
        assert key in data


def test_usage_endpoint_rejects_out_of_range_days(client):
    assert client.get("/api/usage?days=0").status_code == 422
    assert client.get("/api/usage?days=999").status_code == 422
