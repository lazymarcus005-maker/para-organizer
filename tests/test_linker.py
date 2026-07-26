"""Semantic auto-linking tests (app.linker). Embeddings are patched to fixed
one-hot vectors (as in test_rag.py) so similarity is deterministic and no live
embedding server is needed."""

import pytest

import app.linker as linker
import app.vector_store as vector_store
from app.database import get_connection
from app.linker import auto_link_note, suggest_links
from app.vector_store import index_note
from tests.conftest import insert_note

AUTH = {"Authorization": "Bearer cron-secret"}


def _vec(index: int, dim: int = 768) -> list:
    v = [0.0] * dim
    v[index] = 1.0
    return v


def _fake_embed(vector_map: dict, default=None):
    async def fake(text: str):
        if text in vector_map:
            return vector_map[text]
        for key, vec in vector_map.items():
            if key in text:
                return vec
        return default
    return fake


def _patch_embed(monkeypatch, vector_map, default=None):
    embed = _fake_embed(vector_map, default)
    monkeypatch.setattr(vector_store, "embed_text", embed)
    monkeypatch.setattr(linker, "embed_text", embed)


async def _index(note_id: int, content: str):
    async with get_connection() as db:
        await index_note(db, note_id, content)


@pytest.mark.asyncio
async def test_suggest_links_returns_similar_notes(test_db, monkeypatch):
    a = await insert_note(title="Note A", content="alpha content")
    b = await insert_note(title="Note B", content="beta content")
    # Both notes share the same embedding, so B is a strong match for A.
    _patch_embed(monkeypatch, {"alpha content": _vec(0), "beta content": _vec(0)})
    await _index(a, "alpha content")
    await _index(b, "beta content")

    result = await suggest_links(a)

    assert [r["note_id"] for r in result] == [b]
    assert result[0]["title"] == "Note B"
    assert result[0]["similarity"] > 0.7


@pytest.mark.asyncio
async def test_suggest_links_filters_below_threshold(test_db, monkeypatch):
    a = await insert_note(content="alpha content")
    b = await insert_note(content="beta content")
    # Orthogonal one-hot vectors → similarity ~0.41, below the 0.7 threshold.
    _patch_embed(monkeypatch, {"alpha content": _vec(0), "beta content": _vec(1)})
    await _index(a, "alpha content")
    await _index(b, "beta content")

    assert await suggest_links(a, similarity_threshold=0.7) == []


@pytest.mark.asyncio
async def test_suggest_links_excludes_self(test_db, monkeypatch):
    a = await insert_note(content="alpha content")
    _patch_embed(monkeypatch, {"alpha content": _vec(0)})
    await _index(a, "alpha content")

    result = await suggest_links(a)
    assert all(r["note_id"] != a for r in result)


@pytest.mark.asyncio
async def test_suggest_links_respects_top_k(test_db, monkeypatch):
    a = await insert_note(content="alpha content")
    others = [await insert_note(title=f"N{i}", content="alpha content") for i in range(4)]
    _patch_embed(monkeypatch, {"alpha content": _vec(0)})
    await _index(a, "alpha content")
    for oid in others:
        await _index(oid, "alpha content")

    result = await suggest_links(a, top_k=2)
    assert len(result) == 2
    assert a not in {r["note_id"] for r in result}


@pytest.mark.asyncio
async def test_auto_link_creates_related_link_and_history(test_db, monkeypatch):
    a = await insert_note(content="alpha content")
    b = await insert_note(content="beta content")
    _patch_embed(monkeypatch, {"alpha content": _vec(0), "beta content": _vec(0)})
    await _index(a, "alpha content")
    await _index(b, "beta content")

    async with get_connection() as db:
        created = await auto_link_note(db, a)
        assert created == 1
        link = await (await db.execute(
            "SELECT from_note_id, to_note_id, link_type FROM links"
        )).fetchone()
        assert (link["from_note_id"], link["to_note_id"], link["link_type"]) == (a, b, "related")
        hist = await (await db.execute(
            "SELECT reason FROM history WHERE note_id=? AND action='auto_linked'", (a,)
        )).fetchone()
        assert hist["reason"] == "auto-linked to 1 notes"


@pytest.mark.asyncio
async def test_auto_link_skips_existing_link_either_direction(test_db, monkeypatch):
    a = await insert_note(content="alpha content")
    b = await insert_note(content="beta content")
    _patch_embed(monkeypatch, {"alpha content": _vec(0), "beta content": _vec(0)})
    await _index(a, "alpha content")
    await _index(b, "beta content")

    async with get_connection() as db:
        # Pre-existing link in the reverse direction (b -> a).
        await db.execute(
            "INSERT INTO links (from_note_id, to_note_id, link_type) VALUES (?, ?, 'related')",
            (b, a),
        )
        await db.commit()
        assert await auto_link_note(db, a) == 0
        count = (await (await db.execute("SELECT COUNT(*) c FROM links")).fetchone())["c"]
        assert count == 1


@pytest.mark.asyncio
async def test_auto_link_noop_when_embedding_unavailable(test_db, monkeypatch):
    a = await insert_note(content="alpha content")
    _patch_embed(monkeypatch, {}, default=None)

    async with get_connection() as db:
        assert await auto_link_note(db, a) == 0


@pytest.mark.asyncio
async def test_create_note_auto_links_via_api(client, monkeypatch):
    _patch_embed(monkeypatch, {"shared topic": _vec(0)})

    first = client.post(
        "/api/notes",
        json={"title": "A", "content": "shared topic", "auto_classify": False},
        headers=AUTH,
    )
    second = client.post(
        "/api/notes",
        json={"title": "B", "content": "shared topic", "auto_classify": False},
        headers=AUTH,
    )
    assert first.status_code == 200 and second.status_code == 200
    id_first, id_second = first.json()["id"], second.json()["id"]

    async with get_connection() as db:
        rows = await (await db.execute("SELECT from_note_id, to_note_id FROM links")).fetchall()

    # The second note is created after the first is indexed, so it links back to it.
    assert any(r["from_note_id"] == id_second and r["to_note_id"] == id_first for r in rows)
