"""SQLite connection management (WAL mode) and schema migrations."""

import logging
import os
from contextlib import asynccontextmanager

import aiosqlite

from app.config import settings
from app.migrations import run_migrations

logger = logging.getLogger("para.database")

try:
    import sqlite_vec
    _SQLITE_VEC_AVAILABLE = True
    logger.info("sqlite-vec extension available")
except ImportError:
    sqlite_vec = None
    _SQLITE_VEC_AVAILABLE = False
    logger.warning("sqlite-vec not available — vector store will not be created (non-blocking)")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    para_category TEXT NOT NULL DEFAULT 'inbox',
    sub_category TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    priority TEXT NOT NULL DEFAULT 'medium',
    deadline DATE,
    tags TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'manual',
    source_metadata TEXT NOT NULL DEFAULT '{}',
    llm_model TEXT,
    llm_confidence REAL NOT NULL DEFAULT 0.0,
    llm_reasoning TEXT,
    embedding_status TEXT DEFAULT 'pending',
    recurrence TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_at DATETIME
);

CREATE INDEX IF NOT EXISTS idx_notes_category ON notes(para_category);
CREATE INDEX IF NOT EXISTS idx_notes_status ON notes(status);
CREATE INDEX IF NOT EXISTS idx_notes_deadline ON notes(deadline);
CREATE INDEX IF NOT EXISTS idx_notes_source ON notes(source);

CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_note_id INTEGER NOT NULL,
    to_note_id INTEGER NOT NULL,
    link_type TEXT NOT NULL DEFAULT 'related',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (from_note_id) REFERENCES notes(id) ON DELETE CASCADE,
    FOREIGN KEY (to_note_id) REFERENCES notes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_links_from ON links(from_note_id);
CREATE INDEX IF NOT EXISTS idx_links_to ON links(to_note_id);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_history_note ON history(note_id);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER,
    type TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'telegram',
    status TEXT NOT NULL DEFAULT 'pending',
    scheduled_at DATETIME NOT NULL,
    sent_at DATETIME,
    payload TEXT,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_notif_status ON notifications(status);
CREATE INDEX IF NOT EXISTS idx_notif_scheduled ON notifications(scheduled_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_chat ON chat_messages(chat_id, id);

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts DATETIME DEFAULT CURRENT_TIMESTAMP,
    model TEXT NOT NULL,
    task TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    note_id INTEGER,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title, content, tags,
    content='notes',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO notes_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;
"""

# Vector store for hybrid RAG semantic search — a separate virtual table (not part
# of SCHEMA_SQL) because it requires the sqlite-vec extension to be loaded on the
# connection *before* it can be created or queried; see _load_vec_extension().
VEC_SCHEMA_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS note_embeddings USING vec0(
    embedding float[{dimensions}]
);
"""


async def _load_vec_extension(db: aiosqlite.Connection) -> bool:
    """Load the sqlite-vec extension onto this connection. Returns False (and logs
    a warning, once) if the package isn't installed or the extension can't load —
    callers degrade to keyword-only (FTS) search in that case."""
    if not _SQLITE_VEC_AVAILABLE:
        return False
    try:
        await db.enable_load_extension(True)
        await db.load_extension(sqlite_vec.loadable_path())
        await db.enable_load_extension(False)
        return True
    except (aiosqlite.Error, AttributeError, OSError):
        logger.warning(
            "Failed to load sqlite-vec extension; semantic search disabled, "
            "falling back to keyword-only retrieval", exc_info=True,
        )
        return False


async def init_db() -> None:
    """Create the database file (if needed) and run migrations."""
    db_path = settings.PARA_DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute("PRAGMA busy_timeout=5000;")
        await db.executescript(SCHEMA_SQL)
        vec_loaded = await _load_vec_extension(db)
        if vec_loaded:
            try:
                await db.executescript(VEC_SCHEMA_SQL.format(dimensions=settings.EMBED_DIMENSIONS))
            except aiosqlite.Error:
                logger.warning("Failed to create note_embeddings vector table", exc_info=True)
        await run_migrations(db)
        await db.commit()
        logger.info(
            "init_db complete (sqlite-vec %s)",
            "loaded" if vec_loaded else "unavailable",
        )


@asynccontextmanager
async def get_connection():
    """Async context manager yielding a configured aiosqlite connection."""
    db = await aiosqlite.connect(settings.PARA_DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute("PRAGMA busy_timeout=5000;")
        await _load_vec_extension(db)
        yield db
    finally:
        await db.close()


async def get_db():
    """FastAPI dependency yielding an aiosqlite connection."""
    async with get_connection() as db:
        yield db
