"""Hybrid RAG tests: embedding provider abstraction (app.embed), the sqlite-vec
vector store (app.vector_store), and the FTS+semantic merge in
app.chat._hybrid_retrieve. Embeddings are patched to fixed vectors so tests
stay deterministic and don't require a live Ollama/embedding server."""

import httpx
import pytest

import app.chat as chat
import app.vector_store as vector_store
from app.chat import _hybrid_retrieve
from app.database import get_connection
from app.vector_store import delete_note_embedding, index_note, semantic_search
from tests.conftest import insert_note

AUTH = {"Authorization": "Bearer cron-secret"}


def _vec(index: int, dim: int = 768) -> list:
    """One-hot vector matching the note_embeddings table's fixed EMBED_DIMENSIONS."""
    v = [0.0] * dim
    v[index] = 1.0
    return v


def _fake_embed(vector_map: dict, default=None):
    """Return a fake `embed_text(text)` that looks up an exact-match vector,
    falling back to substring containment, then to `default`."""
    async def fake(text: str):
        if text in vector_map:
            return vector_map[text]
        for key, vec in vector_map.items():
            if key in text:
                return vec
        return default
    return fake


# ─── app.embed ───

class _FakeResponse:
    def __init__(self, json_body, status=200):
        self._json = json_body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._json


@pytest.mark.asyncio
async def test_embed_text_parses_ollama_native_response(monkeypatch):
    from app import embed

    monkeypatch.setattr(embed.settings, "EMBED_PROVIDER", "ollama_local")

    async def fake_post(self, url, json=None, headers=None):
        assert url.endswith("/api/embed")
        return _FakeResponse({"embeddings": [[0.1, 0.2, 0.3]]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await embed.embed_text("hello")
    assert result == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embed_text_parses_openai_compatible_response(monkeypatch):
    from app import embed

    monkeypatch.setattr(embed.settings, "EMBED_PROVIDER", "openai_compatible")

    async def fake_post(self, url, json=None, headers=None):
        assert url.endswith("/v1/embeddings")
        return _FakeResponse({"data": [{"embedding": [0.4, 0.5]}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await embed.embed_text("hello")
    assert result == [0.4, 0.5]


@pytest.mark.asyncio
async def test_embed_text_returns_none_on_timeout(monkeypatch):
    from app import embed

    async def fake_post(self, url, json=None, headers=None):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    assert await embed.embed_text("hello") is None


@pytest.mark.asyncio
async def test_embed_batch_collects_each_result(monkeypatch):
    from app import embed

    calls = []

    async def fake_embed_text(text):
        calls.append(text)
        return None if text == "bad" else [1.0]

    monkeypatch.setattr(embed, "embed_text", fake_embed_text)
    result = await embed.embed_batch(["good", "bad"])
    assert result == [[1.0], None]
    assert calls == ["good", "bad"]


# ─── app.vector_store (real sqlite-vec round-trip) ───

@pytest.mark.asyncio
async def test_index_and_semantic_search_round_trip(test_db, monkeypatch):
    note_id = await insert_note(title="Server", content="server maintenance log")
    monkeypatch.setattr(vector_store, "embed_text", _fake_embed({"server maintenance log": _vec(0)}))

    async with get_connection() as db:
        await index_note(db, note_id, "server maintenance log")
        matches = await semantic_search(db, _vec(0), limit=5)

    assert matches
    assert matches[0][0] == note_id
    assert 0.0 < matches[0][1] <= 1.0


@pytest.mark.asyncio
async def test_index_note_is_noop_when_embedding_fails(test_db, monkeypatch):
    note_id = await insert_note()
    monkeypatch.setattr(vector_store, "embed_text", _fake_embed({}, default=None))

    async with get_connection() as db:
        await index_note(db, note_id, "anything")
        matches = await semantic_search(db, _vec(0), limit=5)

    assert matches == []


@pytest.mark.asyncio
async def test_delete_note_embedding_removes_vector(test_db, monkeypatch):
    note_id = await insert_note()
    monkeypatch.setattr(vector_store, "embed_text", _fake_embed({"anything": _vec(0)}))

    async with get_connection() as db:
        await index_note(db, note_id, "anything")
        assert await semantic_search(db, _vec(0), limit=5)

        await delete_note_embedding(db, note_id)
        assert await semantic_search(db, _vec(0), limit=5) == []


# ─── app.chat._hybrid_retrieve (merge) ───

@pytest.mark.asyncio
async def test_hybrid_retrieve_merges_fts_and_semantic_hits(test_db, monkeypatch):
    fts_note_id = await insert_note(title="Server log", content="server maintenance schedule")
    semantic_note_id = await insert_note(title="Budget", content="quarterly budget planning")

    monkeypatch.setattr(vector_store, "embed_text", _fake_embed({
        "server maintenance schedule": _vec(0),
        "quarterly budget planning": _vec(1),
    }))
    async with get_connection() as db:
        await index_note(db, fts_note_id, "server maintenance schedule")
        await index_note(db, semantic_note_id, "quarterly budget planning")

    # Query keyword-matches the FTS note ("server") but its embedding is
    # deliberately made to point at the *other* note's vector, so the semantic
    # note only appears via vector search, not FTS.
    monkeypatch.setattr(chat, "embed_text", _fake_embed({"server": _vec(1)}))

    async with get_connection() as db:
        results = await _hybrid_retrieve("server", db)

    result_ids = {row["id"] for row in results}
    assert fts_note_id in result_ids
    assert semantic_note_id in result_ids


@pytest.mark.asyncio
async def test_hybrid_retrieve_falls_back_to_fts_when_embedding_fails(test_db, monkeypatch):
    note_id = await insert_note(title="Server log", content="server maintenance schedule")
    monkeypatch.setattr(chat, "embed_text", _fake_embed({}, default=None))

    async with get_connection() as db:
        results = await _hybrid_retrieve("server", db)

    assert [row["id"] for row in results] == [note_id]


@pytest.mark.asyncio
async def test_hybrid_retrieve_disabled_skips_semantic_search(test_db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "RAG_HYBRID_ENABLED", False)
    calls = []

    async def fake_embed_text(text):
        calls.append(text)
        return _vec(0)

    monkeypatch.setattr(chat, "embed_text", fake_embed_text)
    await insert_note(title="Server log", content="server maintenance schedule")

    async with get_connection() as db:
        await _hybrid_retrieve("server", db)

    assert calls == []


# ─── auto-indexing via the notes API ───

@pytest.mark.asyncio
async def test_create_note_indexes_embedding(client, monkeypatch):
    monkeypatch.setattr(vector_store, "embed_text", _fake_embed({"server maintenance log": _vec(0)}))

    resp = client.post(
        "/api/notes",
        json={"title": "Server", "content": "server maintenance log", "auto_classify": False},
        headers=AUTH,
    )
    assert resp.status_code == 200
    note_id = resp.json()["id"]

    async with get_connection() as db:
        matches = await semantic_search(db, _vec(0), limit=5)
    assert matches and matches[0][0] == note_id


@pytest.mark.asyncio
async def test_update_note_reindexes_embedding(client, monkeypatch):
    seen_content = []

    async def fake_embed_text(text):
        seen_content.append(text)
        return _vec(0)

    monkeypatch.setattr(vector_store, "embed_text", fake_embed_text)

    resp = client.post(
        "/api/notes",
        json={"title": "Server", "content": "original content", "auto_classify": False},
        headers=AUTH,
    )
    note_id = resp.json()["id"]

    client.put(f"/api/notes/{note_id}", json={"content": "updated content"})

    assert "original content" in seen_content
    assert "updated content" in seen_content


@pytest.mark.asyncio
async def test_delete_note_removes_embedding(client, monkeypatch):
    monkeypatch.setattr(vector_store, "embed_text", _fake_embed({"server maintenance log": _vec(0)}))

    resp = client.post(
        "/api/notes",
        json={"title": "Server", "content": "server maintenance log", "auto_classify": False},
        headers=AUTH,
    )
    note_id = resp.json()["id"]

    client.delete(f"/api/notes/{note_id}")

    async with get_connection() as db:
        matches = await semantic_search(db, _vec(0), limit=5)
    assert matches == []
