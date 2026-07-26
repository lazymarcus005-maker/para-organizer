"""Unit tests for the PARA Organizer MCP server tools.

The database is a throw-away SQLite file per test (via the ``db`` fixture) and
the LLM classifier is mocked so no network calls are made.
"""

import json
from datetime import date, timedelta

import pytest
import pytest_asyncio

from app.config import settings
from app.database import get_connection, init_db
from app.mcp import mcp_server
from app.mcp.mcp_server import (
    mcp,
    para_add_link,
    para_add_note,
    para_archive,
    para_complete,
    para_deadlines,
    para_delete,
    para_digest,
    para_get,
    para_list,
    para_move,
    para_reclassify,
    para_search,
    para_stats,
    para_update,
)

ALL_TOOLS = {
    "para_add_note", "para_search", "para_list", "para_get", "para_move",
    "para_archive", "para_stats", "para_deadlines", "para_digest", "para_add_link",
}


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """Point the app at a fresh SQLite file and create the schema."""
    monkeypatch.setattr(settings, "PARA_DB_PATH", str(tmp_path / "test.db"))
    await init_db()
    yield


@pytest.fixture
def mock_classifier(monkeypatch):
    """Patch the classifier used by mcp_server to a deterministic fake."""
    async def fake_classify(title, content):
        return {
            "para_category": "projects",
            "sub_category": "Vehicle Registration",
            "priority": "high",
            "deadline": "2025-08-15",
            "tags": ["รถยนต์", "เอกสาร", "deadline"],
            "confidence": 0.95,
            "llm_model": "test-model",
            "reasoning": "มีกำหนดเวลาชัดเจน",
        }
    monkeypatch.setattr(mcp_server, "classify_note", fake_classify)
    return fake_classify


async def make_note(title="note", content="body", para_category="projects",
                    status="active", priority="medium", deadline=None,
                    tags=None, source="manual") -> int:
    """Insert a note directly and return its id."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """INSERT INTO notes (title, content, para_category, status, priority,
                                  deadline, tags, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, content, para_category, status, priority, deadline,
             json.dumps(tags or [], ensure_ascii=False), source),
        )
        await conn.commit()
        return cursor.lastrowid


@pytest.mark.asyncio
async def test_all_ten_tools_registered(db):
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == ALL_TOOLS
    assert len(tools) == 15  # Now 15 tools: 10 original + para_update, para_complete, para_delete, para_reclassify, para_ask


@pytest.mark.asyncio
async def test_add_note_classifies_and_saves(db, mock_classifier):
    note = await para_add_note("ต่อทะเบียนรถ", "ทะเบียนหมดอายุ 15 สิงหาคม 2025")
    assert note["id"] >= 1
    assert note["title"] == "ต่อทะเบียนรถ"
    assert note["para_category"] == "projects"
    assert note["priority"] == "high"
    assert note["deadline"] == "2025-08-15"
    assert note["source"] == "hermes"
    assert note["status"] == "active"
    assert note["llm_model"] == "test-model"
    assert note["llm_confidence"] == 0.95
    assert "รถยนต์" in note["tags"]
    assert note["llm_reasoning"] == "มีกำหนดเวลาชัดเจน"


@pytest.mark.asyncio
async def test_add_note_falls_back_to_inbox(db, monkeypatch):
    from app.classifier import DEFAULT_RESULT

    async def fake_classify(title, content):
        return dict(DEFAULT_RESULT)

    monkeypatch.setattr(mcp_server, "classify_note", fake_classify)
    note = await para_add_note("whatever", "no classification available")
    assert note["para_category"] == "inbox"
    assert note["llm_confidence"] == 0.0
    assert note["llm_model"] is None


@pytest.mark.asyncio
async def test_add_note_extracts_deadline_when_llm_misses(db, monkeypatch):
    async def fake_classify(title, content):
        return {
            "para_category": "projects", "sub_category": None, "priority": "medium",
            "deadline": None, "tags": [], "confidence": 0.8,
            "llm_model": "m", "reasoning": "",
        }

    monkeypatch.setattr(mcp_server, "classify_note", fake_classify)
    note = await para_add_note("Report", "submit the report by 2025-08-15")
    assert note["deadline"] == "2025-08-15"


@pytest.mark.asyncio
async def test_add_note_thai_roundtrip(db, mock_classifier):
    note = await para_add_note("ดูแลเซิร์ฟเวอร์ Contabo", "ดูแล server ทุกตัว เป็นงานประจำ")
    fetched = await para_get(note["id"])
    assert fetched["title"] == "ดูแลเซิร์ฟเวอร์ Contabo"
    assert fetched["content"] == "ดูแล server ทุกตัว เป็นงานประจำ"


@pytest.mark.asyncio
async def test_search_returns_ranked_matches(db):
    await make_note(title="Server Monitoring", content="check contabo server health", para_category="areas")
    await make_note(title="Recipe", content="pad krapow pork basil", para_category="resources")
    results = await para_search("contabo")
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["title"] == "Server Monitoring"
    assert "rank" in results[0]


@pytest.mark.asyncio
async def test_search_with_category_filter(db):
    await make_note(title="A", content="contabo one", para_category="areas")
    await make_note(title="B", content="contabo two", para_category="resources")
    results = await para_search("contabo", category="resources")
    assert len(results) == 1
    assert results[0]["para_category"] == "resources"
    assert results[0]["title"] == "B"


@pytest.mark.asyncio
async def test_search_respects_limit(db):
    for i in range(5):
        await make_note(title=f"Note {i}", content="contabo cluster node")
    results = await para_search("contabo", limit=3)
    assert len(results) == 3


@pytest.mark.asyncio
async def test_search_malformed_query_returns_error(db):
    result = await para_search('"')
    assert isinstance(result, dict)
    assert "error" in result


@pytest.mark.asyncio
async def test_list_all_and_filtered(db):
    await make_note(title="P1", para_category="projects")
    await make_note(title="A1", para_category="areas")
    await make_note(title="P2", para_category="projects", status="completed")

    assert len(await para_list()) == 3
    assert len(await para_list(category="projects")) == 2
    assert len(await para_list(category="areas")) == 1

    active_projects = await para_list(category="projects", status="active")
    assert len(active_projects) == 1
    assert active_projects[0]["title"] == "P1"


@pytest.mark.asyncio
async def test_list_respects_limit(db):
    for i in range(4):
        await make_note(title=f"N{i}")
    assert len(await para_list(limit=2)) == 2


@pytest.mark.asyncio
async def test_get_returns_note(db):
    note_id = await make_note(title="GetMe", content="hello", tags=["a", "b"])
    note = await para_get(note_id)
    assert note["id"] == note_id
    assert note["title"] == "GetMe"
    assert note["tags"] == ["a", "b"]


@pytest.mark.asyncio
async def test_get_not_found(db):
    result = await para_get(9999)
    assert result == {"error": "Note 9999 not found"}


@pytest.mark.asyncio
async def test_move_changes_category(db):
    note_id = await make_note(title="M", para_category="projects")
    moved = await para_move(note_id, "resources")
    assert moved["para_category"] == "resources"
    assert (await para_get(note_id))["para_category"] == "resources"


@pytest.mark.asyncio
async def test_move_invalid_category(db):
    note_id = await make_note(title="M")
    result = await para_move(note_id, "bogus")
    assert "error" in result
    assert (await para_get(note_id))["para_category"] == "projects"


@pytest.mark.asyncio
async def test_move_not_found(db):
    result = await para_move(9999, "projects")
    assert "error" in result


@pytest.mark.asyncio
async def test_archive_sets_status_and_category(db):
    note_id = await make_note(title="Done", para_category="projects")
    archived = await para_archive(note_id)
    assert archived["status"] == "archived"
    assert archived["para_category"] == "archives"
    assert archived["archived_at"] is not None


@pytest.mark.asyncio
async def test_archive_not_found(db):
    result = await para_archive(9999)
    assert "error" in result


@pytest.mark.asyncio
async def test_stats_counts(db):
    await make_note(title="P", para_category="projects", priority="high")
    await make_note(title="A", para_category="areas", priority="low")
    await make_note(title="R", para_category="resources", priority="medium")

    stats = await para_stats()
    assert stats["total_notes"] == 3
    assert stats["by_category"] == {"projects": 1, "areas": 1, "resources": 1}
    assert stats["by_status"]["active"] == 3
    assert stats["by_priority"]["high"] == 1
    assert stats["by_priority"]["low"] == 1
    assert "avg_confidence" in stats
    assert "upcoming_deadlines" in stats


@pytest.mark.asyncio
async def test_stats_empty_db(db):
    stats = await para_stats()
    assert stats["total_notes"] == 0
    assert stats["by_category"] == {}
    assert stats["avg_confidence"] == 0.0


@pytest.mark.asyncio
async def test_deadlines_within_window(db):
    soon = (date.today() + timedelta(days=5)).isoformat()
    far = (date.today() + timedelta(days=30)).isoformat()
    overdue = (date.today() - timedelta(days=3)).isoformat()

    await make_note(title="Soon", deadline=soon, priority="high")
    await make_note(title="Far", deadline=far)
    await make_note(title="Overdue", deadline=overdue)

    result = await para_deadlines(days_ahead=14)
    titles = [d["title"] for d in result]
    assert "Soon" in titles
    assert "Far" not in titles
    assert "Overdue" not in titles

    soon_item = next(d for d in result if d["title"] == "Soon")
    assert soon_item["days_left"] == 5
    assert soon_item["priority"] == "high"
    assert soon_item["deadline"] == soon


@pytest.mark.asyncio
async def test_deadlines_ignores_archived(db):
    soon = (date.today() + timedelta(days=2)).isoformat()
    await make_note(title="ArchivedDL", deadline=soon, status="archived")
    result = await para_deadlines(days_ahead=14)
    assert all(d["title"] != "ArchivedDL" for d in result)


@pytest.mark.asyncio
async def test_digest_summarises_week(db):
    await make_note(title="ActiveProj", para_category="projects", status="active")
    await make_note(title="DoneProj", para_category="projects", status="archived")
    await make_note(title="SomeArea", para_category="areas", status="active")

    digest = await para_digest()
    assert digest["total_notes"] == 3
    assert digest["by_category"]["projects"] == 2
    assert digest["by_category"]["areas"] == 1

    active_titles = [p["title"] for p in digest["active_projects"]]
    assert "ActiveProj" in active_titles
    assert "DoneProj" not in active_titles

    completed_titles = [c["title"] for c in digest["completed_this_week"]]
    assert "DoneProj" in completed_titles

    new_titles = [n["title"] for n in digest["new_notes_this_week"]]
    assert "ActiveProj" in new_titles
    assert "SomeArea" in new_titles

    assert isinstance(digest["stale_projects"], list)


@pytest.mark.asyncio
async def test_add_link_default_type(db):
    a = await make_note(title="A")
    b = await make_note(title="B")
    link = await para_add_link(a, b)
    assert link["id"] >= 1
    assert link["from_note_id"] == a
    assert link["to_note_id"] == b
    assert link["link_type"] == "related"


@pytest.mark.asyncio
async def test_add_link_custom_type(db):
    a = await make_note(title="A")
    b = await make_note(title="B")
    link = await para_add_link(a, b, link_type="depends_on")
    assert link["link_type"] == "depends_on"


@pytest.mark.asyncio
async def test_add_link_invalid_type(db):
    a = await make_note(title="A")
    b = await make_note(title="B")
    result = await para_add_link(a, b, link_type="bogus")
    assert "error" in result


@pytest.mark.asyncio
async def test_add_link_missing_note(db):
    a = await make_note(title="A")
    result = await para_add_link(a, 9999)
    assert "error" in result


@pytest.mark.asyncio
async def test_add_link_self_not_allowed(db):
    a = await make_note(title="A")
    result = await para_add_link(a, a)
    assert "error" in result


@pytest.mark.asyncio
async def test_tool_callable_through_mcp_protocol(db):
    await make_note(title="Proto", para_category="projects")
    result = await mcp.call_tool("para_stats", {})
    assert isinstance(result, list)
    data = json.loads(result[0].text)
    assert data["total_notes"] == 1
    assert data["by_category"]["projects"] == 1


# Tests for new Phase 1 tools


@pytest.mark.asyncio
async def test_para_update_single_field(db):
    note_id = await make_note(title="Original Title", content="Original content", priority="low")
    updated = await para_update(note_id, title="New Title")
    assert updated["id"] == note_id
    assert updated["title"] == "New Title"
    assert updated["content"] == "Original content"  # unchanged
    assert updated["priority"] == "low"  # unchanged


@pytest.mark.asyncio
async def test_para_update_multiple_fields(db):
    note_id = await make_note(title="Original", content="body", priority="low")
    updated = await para_update(note_id, title="New Title", priority="high", tags=["tag1", "tag2"])
    assert updated["title"] == "New Title"
    assert updated["priority"] == "high"
    assert updated["tags"] == ["tag1", "tag2"]


@pytest.mark.asyncio
async def test_para_update_with_deadline(db):
    note_id = await make_note(title="Task", content="do this")
    updated = await para_update(note_id, deadline="2025-12-31")
    assert updated["deadline"] == "2025-12-31"


@pytest.mark.asyncio
async def test_para_update_not_found(db):
    result = await para_update(9999, title="New Title")
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_para_update_no_changes(db):
    note_id = await make_note(title="Test", content="body")
    updated = await para_update(note_id)
    assert updated["id"] == note_id
    assert updated["title"] == "Test"


@pytest.mark.asyncio
async def test_para_update_logs_history(db):
    note_id = await make_note(title="Original", content="body")
    await para_update(note_id, title="Updated")
    
    # Check that history was logged
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT action, old_value, new_value FROM history WHERE note_id = ? AND action = 'updated'",
            (note_id,),
        )
        rows = await cursor.fetchall()
        assert len(rows) > 0
        assert any(r["new_value"] == "Updated" for r in rows)


@pytest.mark.asyncio
async def test_para_complete_changes_status(db):
    note_id = await make_note(title="Task", status="active")
    completed = await para_complete(note_id)
    assert completed["status"] == "completed"
    assert (await para_get(note_id))["status"] == "completed"


@pytest.mark.asyncio
async def test_para_complete_not_found(db):
    result = await para_complete(9999)
    assert "error" in result


@pytest.mark.asyncio
async def test_para_complete_logs_history(db):
    note_id = await make_note(title="Task")
    await para_complete(note_id)
    
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT action, new_value FROM history WHERE note_id = ? AND action = 'completed'",
            (note_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["action"] == "completed"


@pytest.mark.asyncio
async def test_para_delete_soft_deletes(db):
    note_id = await make_note(title="Delete me", content="soon")
    result = await para_delete(note_id)
    assert result == {"deleted": note_id}
    
    # Verify archived_at is set
    fetched = await para_get(note_id)
    assert fetched["id"] == note_id
    assert fetched["archived_at"] is not None


@pytest.mark.asyncio
async def test_para_delete_not_found(db):
    result = await para_delete(9999)
    assert "error" in result


@pytest.mark.asyncio
async def test_para_delete_logs_history(db):
    note_id = await make_note(title="Task")
    await para_delete(note_id)
    
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT action FROM history WHERE note_id = ? AND action = 'deleted'",
            (note_id,),
        )
        row = await cursor.fetchone()
        assert row is not None


@pytest.mark.asyncio
async def test_para_reclassify_updates_category(db, mock_classifier):
    note_id = await make_note(title="Server task", content="maintain contabo servers", 
                              para_category="inbox", priority="low")
    reclassified = await para_reclassify(note_id)
    
    # mock_classifier returns "projects" with "high" priority
    assert reclassified["para_category"] == "projects"
    assert reclassified["priority"] == "high"
    assert reclassified["deadline"] == "2025-08-15"


@pytest.mark.asyncio
async def test_para_reclassify_updates_tags(db, mock_classifier):
    note_id = await make_note(title="Task", tags=[])
    reclassified = await para_reclassify(note_id)
    
    # mock_classifier returns tags with Thai/English mix
    assert "รถยนต์" in reclassified["tags"]
    assert "เอกสาร" in reclassified["tags"]


@pytest.mark.asyncio
async def test_para_reclassify_not_found(db):
    result = await para_reclassify(9999)
    assert "error" in result


@pytest.mark.asyncio
async def test_para_reclassify_logs_history(db, mock_classifier):
    note_id = await make_note(title="Task", para_category="inbox")
    await para_reclassify(note_id)
    
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT action, new_value FROM history WHERE note_id = ? AND action = 'reclassified'",
            (note_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["action"] == "reclassified"
        assert row["new_value"] == "projects"

