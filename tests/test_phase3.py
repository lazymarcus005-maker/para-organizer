"""Tests for Phase 3: IMP-04 (para_ask MCP tool) and IMP-05 (note distillation on archive)."""

import json
from datetime import date

import pytest
import pytest_asyncio

from app.config import settings
from app.database import get_connection, init_db
from app.distill import distill_note
from app.mcp.mcp_server import (
    mcp,
    para_add_note,
    para_archive,
    para_ask,
    para_get,
)


# Database fixture
@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """Set up a test database."""
    monkeypatch.setattr(settings, "PARA_DB_PATH", str(tmp_path / "test.db"))
    await init_db()
    yield


# Classifier mock
@pytest.fixture
def mock_classifier(monkeypatch):
    """Patch the classifier to return deterministic results."""
    async def fake_classify(title, content):
        return {
            "para_category": "projects",
            "sub_category": "Test",
            "priority": "high",
            "deadline": "2025-12-31",
            "tags": ["test", "phase3"],
            "confidence": 0.9,
            "llm_model": "test-model",
            "reasoning": "Test note",
        }
    from app.mcp import mcp_server
    from app.classifier import classify_note
    monkeypatch.setattr("app.mcp.mcp_server.classify_note", fake_classify)
    monkeypatch.setattr("app.classifier.classify_note", fake_classify)
    return fake_classify


# LLM mock
@pytest.fixture
def mock_llm(monkeypatch):
    """Mock call_ollama for predictable responses in distill and para_ask."""
    async def fake_call_ollama(model, messages=None, format=None, task=None):
        if task == "distill":
            return "This is a key lesson from the archived note."
        elif task == "ask":
            return "Based on your notes, here is the answer to your question."
        return "Generic response"
    
    monkeypatch.setattr("app.distill.call_ollama", fake_call_ollama)
    monkeypatch.setattr("app.classifier.call_ollama", fake_call_ollama)
    return fake_call_ollama


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
        note_id = cursor.lastrowid
        assert note_id is not None
        return note_id


# ============================================================================
# IMP-04 Tests: para_ask MCP Tool
# ============================================================================

@pytest.mark.asyncio
async def test_para_ask_tool_registered(db):
    """Verify para_ask tool is registered with MCP."""
    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}
    assert "para_ask" in tool_names


@pytest.mark.asyncio
async def test_para_ask_returns_answer_and_sources(db, mock_llm):
    """para_ask returns dict with 'answer' and 'sources' keys."""
    # Create some test notes
    note1_id = await make_note(
        title="Python Tips",
        content="Use list comprehensions for efficient filtering",
        para_category="resources"
    )
    
    result = await para_ask("What are Python best practices?")
    
    assert isinstance(result, dict)
    assert "answer" in result
    assert "sources" in result
    assert isinstance(result["sources"], list)


@pytest.mark.asyncio
async def test_para_ask_with_no_matching_notes(db, mock_llm):
    """para_ask handles gracefully when no notes match."""
    result = await para_ask("Tell me about quantum computing")
    
    assert result["answer"] == "ไม่พบโน้ตที่เกี่ยวข้องกับคำถามของคุณ"
    assert result["sources"] == []


@pytest.mark.asyncio
async def test_para_ask_sources_include_metadata(db, mock_llm):
    """Sources in para_ask response include note_id, title, relevance, para_category."""
    note_id = await make_note(
        title="Server Maintenance",
        content="Check server health weekly",
        para_category="areas"
    )
    
    result = await para_ask("How do I maintain servers?")
    
    if result["sources"]:
        source = result["sources"][0]
        assert "note_id" in source
        assert "title" in source
        assert "relevance" in source
        assert "para_category" in source
        assert isinstance(source["relevance"], (int, float))
        assert 0 <= source["relevance"] <= 1.0


# ============================================================================
# IMP-05 Tests: Note Distillation on Archive
# ============================================================================

@pytest.mark.asyncio
async def test_distill_note_generates_summary(db, mock_llm):
    """distill_note generates a 1-line summary."""
    note_id = await make_note(
        title="Project Alpha",
        content="We completed the user authentication system. Key learnings: use JWT tokens, always validate input."
    )
    
    async with get_connection() as conn:
        summary = await distill_note(conn, note_id)
    
    assert summary is not None
    assert isinstance(summary, str)
    assert len(summary) > 0


@pytest.mark.asyncio
async def test_distill_note_handles_missing_note(db, mock_llm):
    """distill_note returns None gracefully for missing note."""
    async with get_connection() as conn:
        summary = await distill_note(conn, 9999)
    
    assert summary is None


@pytest.mark.asyncio
async def test_para_archive_stores_summary_in_db(db, mock_llm):
    """para_archive stores the generated summary in the notes table."""
    note_id = await make_note(
        title="Completed Task",
        content="Finished implementing the feature",
        para_category="projects",
        status="active"
    )
    
    # Archive the note
    archived = await para_archive(note_id)
    
    assert archived["status"] == "archived"
    assert archived["para_category"] == "archives"
    
    # Fetch and verify summary is stored
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT summary FROM notes WHERE id = ?", (note_id,))
        row = await cursor.fetchone()
    
    assert row is not None
    # Summary should be set (unless LLM failed)
    assert row["summary"] is not None


@pytest.mark.asyncio
async def test_para_archive_with_successful_distillation(db, mock_llm):
    """para_archive successfully generates and stores a summary."""
    note_id = await make_note(
        title="Server Setup",
        content="Configured Nginx reverse proxy. Installed SSL certificates. Set up WAL mode for SQLite.",
        para_category="projects"
    )
    
    archived = await para_archive(note_id)
    
    assert archived["id"] == note_id
    assert archived["status"] == "archived"
    
    # Verify summary is in the returned note
    assert archived.get("summary") is not None


@pytest.mark.asyncio
async def test_para_archive_sets_archived_timestamp(db, mock_llm):
    """para_archive sets archived_at timestamp."""
    note_id = await make_note(title="Task", para_category="projects")
    
    before = date.today()
    archived = await para_archive(note_id)
    after = date.today()
    
    assert archived["archived_at"] is not None
    # Parse ISO date
    archived_date = date.fromisoformat(archived["archived_at"][:10])
    assert before <= archived_date <= after


@pytest.mark.asyncio
async def test_para_archive_not_found(db, mock_llm):
    """para_archive returns error for missing note."""
    result = await para_archive(9999)
    assert "error" in result


@pytest.mark.asyncio
async def test_summary_column_exists_after_migration(db):
    """summary column exists in notes table after migration."""
    async with get_connection() as conn:
        cursor = await conn.execute("PRAGMA table_info(notes)")
        rows = await cursor.fetchall()
        columns = {row["name"] for row in rows}
    
    assert "summary" in columns


@pytest.mark.asyncio
async def test_archived_notes_persist_summary_on_retrieval(db, mock_llm):
    """Archived note with summary can be retrieved with summary intact."""
    note_id = await make_note(
        title="Learning",
        content="Learned about async/await patterns and their benefits",
        para_category="resources"
    )
    
    # Archive
    archived = await para_archive(note_id)
    
    # Fetch
    fetched = await para_get(note_id)
    
    assert fetched["id"] == note_id
    assert fetched["para_category"] == "archives"
    assert fetched["status"] == "archived"
    # Summary should be set
    assert fetched["summary"] is not None
    assert fetched["summary"] == archived["summary"]


# ============================================================================
# Integration Tests
# ============================================================================

@pytest.mark.asyncio
async def test_integration_archive_creates_summary(db, mock_llm):
    """Full integration: archive creates and stores summary."""
    # Create a note
    note_id = await make_note(
        title="Important Lesson",
        content="Always validate user input before processing",
        para_category="resources"
    )
    
    # Archive it
    archived = await para_archive(note_id)
    
    # Verify it's archived with summary
    assert archived["status"] == "archived"
    assert archived["para_category"] == "archives"
    assert archived["summary"] is not None
    
    # Verify in DB
    fetched = await para_get(note_id)
    assert fetched["summary"] == archived["summary"]
