"""Confidence-based inbox routing tests (IMP-18). classify_note is exercised with
a mocked LLM response; the create-note route is checked for the review reason log."""

import json

import pytest

import app.classifier as classifier
import app.routes.notes as notes_route
from app.classifier import classify_note
from app.database import get_connection

AUTH = {"Authorization": "Bearer cron-secret"}


def _mock_ollama(result_dict):
    async def fake(model, prompt=None, format="json", messages=None, task="chat", note_id=None):
        return json.dumps(result_dict)
    return fake


BASE_RESULT = {
    "para_category": "projects",
    "sub_category": "x",
    "priority": "high",
    "deadline": None,
    "tags": ["a"],
    "reasoning": "clear goal",
}


@pytest.mark.asyncio
async def test_low_confidence_routes_to_inbox(monkeypatch):
    monkeypatch.setattr(classifier, "call_ollama", _mock_ollama({**BASE_RESULT, "confidence": 0.6}))

    result = await classify_note("t", "c")

    assert result["para_category"] == "inbox"
    assert result["review_needed"] is True
    assert "low confidence" in result["reasoning"]
    # The original model reasoning is preserved alongside the routing reason.
    assert "clear goal" in result["reasoning"]


@pytest.mark.asyncio
async def test_high_confidence_keeps_category(monkeypatch):
    monkeypatch.setattr(classifier, "call_ollama", _mock_ollama({**BASE_RESULT, "confidence": 0.9}))

    result = await classify_note("t", "c")

    assert result["para_category"] == "projects"
    assert result["review_needed"] is False
    assert result["reasoning"] == "clear goal"


@pytest.mark.asyncio
async def test_confidence_at_threshold_is_not_flagged(monkeypatch):
    # Threshold default is 0.7; exactly-0.7 is >= threshold, so it is not routed.
    monkeypatch.setattr(classifier, "call_ollama", _mock_ollama({**BASE_RESULT, "confidence": 0.7}))

    result = await classify_note("t", "c")
    assert result["para_category"] == "projects"
    assert result["review_needed"] is False


@pytest.mark.asyncio
async def test_create_note_low_confidence_logs_reason_and_flags(client, monkeypatch):
    async def classify(title, content):
        return {
            "para_category": "inbox",
            "sub_category": None,
            "priority": "medium",
            "deadline": None,
            "tags": [],
            "confidence": 0.6,
            "llm_model": "mock-model",
            "reasoning": "low confidence (0.60), needs review — maybe a project",
            "review_needed": True,
        }

    monkeypatch.setattr(notes_route, "classify_note", classify)

    resp = client.post("/api/notes", json={"title": "T", "content": "C"}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["para_category"] == "inbox"
    assert body["review_needed"] is True
    note_id = body["id"]

    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT reason FROM history WHERE note_id=? AND action='classified'", (note_id,)
        )).fetchone()
    assert "low confidence" in row["reason"]


@pytest.mark.asyncio
async def test_create_note_high_confidence_not_flagged(client, monkeypatch):
    async def classify(title, content):
        return {
            "para_category": "projects",
            "sub_category": "x",
            "priority": "high",
            "deadline": None,
            "tags": ["a"],
            "confidence": 0.95,
            "llm_model": "mock-model",
            "reasoning": "clear",
            "review_needed": False,
        }

    monkeypatch.setattr(notes_route, "classify_note", classify)

    resp = client.post("/api/notes", json={"title": "T", "content": "C"}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["para_category"] == "projects"
    assert body["review_needed"] is False
