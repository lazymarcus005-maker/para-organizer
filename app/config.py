"""Application settings loaded from environment / .env file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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


settings = Settings()
