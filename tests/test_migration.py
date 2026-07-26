"""F0-1: migration framework + schema additions.

Verifies init_db() is idempotent and that migration 001 adds the notes columns,
the llm_usage table, and the sqlite-vec vector table on both fresh and existing DBs.
"""

import aiosqlite
import pytest

from app.database import get_connection, init_db
from tests.conftest import insert_note

# Expected llm_usage columns, per the F0-1 spec.
LLM_USAGE_COLUMNS = {
    "id", "ts", "model", "task", "prompt_tokens", "completion_tokens", "note_id"
}


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (table,),
    )
    return await cursor.fetchone() is not None


@pytest.mark.asyncio
async def test_new_notes_columns_exist(test_db):
    async with get_connection() as db:
        cols = await _columns(db, "notes")
    assert "embedding_status" in cols
    assert "recurrence" in cols


@pytest.mark.asyncio
async def test_embedding_status_defaults_to_pending(test_db):
    note_id = await insert_note()
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT embedding_status, recurrence FROM notes WHERE id = ?", (note_id,)
        )).fetchone()
    assert row["embedding_status"] == "pending"
    assert row["recurrence"] is None


@pytest.mark.asyncio
async def test_llm_usage_table_schema(test_db):
    async with get_connection() as db:
        assert await _table_exists(db, "llm_usage")
        cols = await _columns(db, "llm_usage")
    assert cols == LLM_USAGE_COLUMNS


@pytest.mark.asyncio
async def test_llm_usage_index_exists(test_db):
    async with get_connection() as db:
        row = await (await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_llm_usage_ts'"
        )).fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_sqlite_vec_extension_and_table_loaded(test_db):
    import sqlite_vec  # ensure the extension package is importable

    assert sqlite_vec is not None
    async with get_connection() as db:
        assert await _table_exists(db, "note_embeddings")
        # A vec_* function is only callable when the extension actually loaded.
        row = await (await db.execute("SELECT vec_version()")).fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_init_db_is_idempotent(test_db):
    # test_db already ran init_db once; running it two more times must not error
    # and must leave the schema intact.
    await init_db()
    await init_db()
    async with get_connection() as db:
        notes_cols = await _columns(db, "notes")
        assert await _table_exists(db, "llm_usage")
    assert {"embedding_status", "recurrence"} <= notes_cols


@pytest.mark.asyncio
async def test_llm_usage_note_id_fk_set_null_on_delete(test_db):
    """note_id uses ON DELETE SET NULL, so usage rows survive note deletion."""
    from app.usage import log_usage

    note_id = await insert_note()
    await log_usage("m", "classify", {"prompt_tokens": 1, "completion_tokens": 1}, note_id)

    async with get_connection() as db:
        await db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        await db.commit()
        row = await (await db.execute(
            "SELECT note_id FROM llm_usage WHERE model = 'm'"
        )).fetchone()
    assert row is not None
    assert row["note_id"] is None
