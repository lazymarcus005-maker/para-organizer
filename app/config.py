"""Application settings loaded from environment / .env file."""

import sqlite3
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

def _cast_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# Settings keys that can be overridden at runtime via PUT /api/settings and are
# persisted in the `settings` table. Kept in sync with app.routes.settings.SETTINGS_KEYS.
_PERSISTED_SETTING_CASTS = {
    "NOTIFY_DEADLINE_DAYS": str,
    "NOTIFY_DIGEST_DAY": str,
    "NOTIFY_DIGEST_TIME": str,
    "NOTIFY_STALE_DAYS": int,
    "AUTO_ARCHIVE_DAYS": int,
    "RECLASSIFY_INTERVAL_HOURS": int,
    "RECLASSIFY_CONFIDENCE_THRESHOLD": float,
    "CHAT_MODEL": str,
    "CHAT_HISTORY_MAX": int,
    "CHAT_SYSTEM_PROMPT": str,
    "RAG_HYBRID_ENABLED": _cast_bool,
    "RAG_HYBRID_RATIO": float,
    "RAG_SEARCH_LIMIT": int,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ─── Core ───
    PARA_PORT: int = 8731
    PARA_DB_PATH: str = str(Path(__file__).resolve().parent.parent / "data" / "para.db")
    PARA_SECRET_KEY: str = "change-me-in-production"

    # ─── LLM (Ollama Cloud) ───
    OLLAMA_API_KEY: str = ""
    OLLAMA_BASE_URL: str = "https://ollama.com/v1"
    LLM_PRIMARY: str = "deepseek-v4-flash"
    LLM_FALLBACK: str = "gpt-oss:20b"
    LLM_TIMEOUT: int = 60
    LLM_MAX_RETRIES: int = 2

    # ─── Telegram ───
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_WEBHOOK_URL: str = ""
    TELEGRAM_WEBHOOK_SECRET: str = ""
    TELEGRAM_ALLOWED_USERS: str = ""

    # ─── Notification ───
    NOTIFY_CHANNEL: str = "telegram"
    NOTIFY_DEADLINE_DAYS: str = "7,3,1"
    NOTIFY_DIGEST_DAY: str = "mon"
    NOTIFY_DIGEST_TIME: str = "08:00"
    NOTIFY_STALE_DAYS: int = 14

    # ─── Auto ───
    AUTO_ARCHIVE_DAYS: int = 30
    RECLASSIFY_INTERVAL_HOURS: int = 6
    RECLASSIFY_CONFIDENCE_THRESHOLD: float = 0.7

    # ─── Web ───
    WEB_PUBLIC_URL: str = "http://localhost:8731"

    # ─── Chat ───
    CHAT_MODEL: str = "gpt-oss:20b"
    CHAT_HISTORY_MAX: int = 20
    CHAT_SYSTEM_PROMPT: str = (
        "คุณคือผู้ช่วย second-brain ของระบบ PARA Organizer "
        "ตอบคำถามโดยอ้างอิงจากโน้ต PARA ของผู้ใช้ (Projects/Areas/Resources/Archives) "
        "ช่วยระดมความคิดและวางแผนงานได้ ตอบกระชับ ตรงประเด็น เป็นภาษาไทยปนอังกฤษ. "
        "You are a helpful PARA second-brain assistant. Ground answers in the user's "
        "PARA notes when relevant, help brainstorm and plan, and keep replies concise, "
        "in Thai and English."
    )

    # ─── Embeddings (hybrid RAG) ───
    EMBED_PROVIDER: str = "ollama_local"
    EMBED_BASE_URL: str = "http://localhost:11434"
    EMBED_MODEL: str = "nomic-embed-text"
    EMBED_API_KEY: str = ""
    EMBED_DIMENSIONS: int = 768
    RAG_HYBRID_ENABLED: bool = True
    RAG_HYBRID_RATIO: float = 0.5
    RAG_SEARCH_LIMIT: int = 5


settings = Settings()


def _load_persisted_overrides() -> None:
    """Apply settings previously saved via PUT /api/settings so they take effect
    immediately on process start (including for values baked into the scheduler's
    cron triggers at import time, e.g. RECLASSIFY_INTERVAL_HOURS).

    Uses the synchronous sqlite3 module (rather than aiosqlite) because this runs
    at import time, before an event loop exists. Best-effort: any failure (missing
    db file, missing table on first run, corrupt value) is ignored and defaults stand.
    """
    db_path = Path(settings.PARA_DB_PATH)
    if not db_path.exists():
        return
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return

    for key, value in rows:
        cast = _PERSISTED_SETTING_CASTS.get(key)
        if cast is None:
            continue
        try:
            setattr(settings, key, cast(value))
        except (ValueError, TypeError):
            continue


_load_persisted_overrides()
