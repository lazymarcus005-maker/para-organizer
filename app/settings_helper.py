"""Helper to categorize and describe PARA Organizer environment settings.

Maps setting names to category groups and human-readable descriptions,
and identifies sensitive fields that should be masked in the UI.
"""

from __future__ import annotations

from typing import Any

# ── Category groups ──────────────────────────────────────────────────────────

SETTING_GROUPS: dict[str, list[str]] = {
    "Core": [
        "PARA_PORT",
        "PARA_DB_PATH",
        "PARA_DB_URL",
        "PARA_REDIS_URL",
        "PARA_DB_POOL_SIZE",
        "PARA_DB_MAX_OVERFLOW",
        "PARA_REDIS_CACHE_TTL",
        "PARA_SECRET_KEY",
    ],
    "LLM (Ollama Cloud)": [
        "OLLAMA_API_KEY",
        "OLLAMA_BASE_URL",
        "LLM_PRIMARY",
        "LLM_FALLBACK",
        "LLM_TIMEOUT",
        "LLM_MAX_RETRIES",
    ],
    "Chat": [
        "CHAT_MODEL",
        "CHAT_HISTORY_MAX",
        "CHAT_SYSTEM_PROMPT",
    ],
    "Embeddings": [
        "EMBED_PROVIDER",
        "EMBED_BASE_URL",
        "EMBED_MODEL",
        "EMBED_API_KEY",
        "EMBED_DIMENSIONS",
        "RAG_HYBRID_ENABLED",
        "RAG_HYBRID_RATIO",
        "RAG_SEARCH_LIMIT",
    ],
    "Telegram": [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_URL",
        "TELEGRAM_WEBHOOK_SECRET",
        "TELEGRAM_ALLOWED_USERS",
    ],
    "Notification": [
        "NOTIFY_CHANNEL",
        "NOTIFY_DEADLINE_DAYS",
        "NOTIFY_DIGEST_DAY",
        "NOTIFY_DIGEST_TIME",
        "NOTIFY_STALE_DAYS",
    ],
    "Auto": [
        "AUTO_ARCHIVE_DAYS",
        "RECLASSIFY_INTERVAL_HOURS",
        "RECLASSIFY_CONFIDENCE_THRESHOLD",
    ],
    "Web": [
        "WEB_PUBLIC_URL",
    ],
    "Cloud Backup": [
        "BACKUP_CLOUD_ENABLED",
        "BACKUP_CLOUD_ENDPOINT",
        "BACKUP_CLOUD_BUCKET",
        "BACKUP_CLOUD_ACCESS_KEY",
        "BACKUP_CLOUD_SECRET_KEY",
        "BACKUP_CLOUD_RETENTION_DAYS",
    ],
    "Event Bus": [
        "EVENT_WEBHOOK_URL",
        "EVENT_WEBHOOK_SECRET",
        "EVENT_TYPES_ENABLED",
        "EVENT_DISPATCH_RETRIES",
    ],
    "Task Delegation": [
        "TASK_AUTO_EXTRACT",
        "TASK_ACTION_VERBS",
    ],
    "Autonomous Tasks": [
        "AUTONOMY_LEVEL",
        "AUTONOMY_DAILY_HOUR",
    ],
}

# ── Human-readable descriptions ─────────────────────────────────────────────

SETTING_DESCRIPTIONS: dict[str, str] = {
    "PARA_PORT": "Port the FastAPI app listens on",
    "PARA_DB_PATH": "Path to the SQLite database file",
    "PARA_DB_URL": "PostgreSQL connection string (production)",
    "PARA_REDIS_URL": "Redis connection string for cache & task queue",
    "PARA_DB_POOL_SIZE": "PostgreSQL connection pool size",
    "PARA_DB_MAX_OVERFLOW": "Max overflow connections for PostgreSQL pool",
    "PARA_REDIS_CACHE_TTL": "Default Redis cache TTL in seconds",
    "PARA_SECRET_KEY": "Bearer token for authenticated API endpoints",
    "OLLAMA_API_KEY": "Ollama Cloud API key for LLM calls",
    "OLLAMA_BASE_URL": "Ollama Cloud API base URL",
    "LLM_PRIMARY": "Primary classification model",
    "LLM_FALLBACK": "Fallback model when primary is unavailable",
    "LLM_TIMEOUT": "LLM request timeout in seconds",
    "LLM_MAX_RETRIES": "Max retries for LLM requests",
    "CHAT_MODEL": "Model used for conversational chat mode",
    "CHAT_HISTORY_MAX": "Max messages kept per chat session",
    "CHAT_SYSTEM_PROMPT": "System prompt for chat mode",
    "EMBED_PROVIDER": "Embedding provider (ollama_local, ollama_cloud, etc.)",
    "EMBED_BASE_URL": "Embedding service base URL",
    "EMBED_MODEL": "Embedding model name",
    "EMBED_API_KEY": "API key for embedding service",
    "EMBED_DIMENSIONS": "Embedding vector dimensions",
    "RAG_HYBRID_ENABLED": "Enable hybrid semantic + keyword search",
    "RAG_HYBRID_RATIO": "Semantic-to-keyword ratio (0.0–1.0)",
    "RAG_SEARCH_LIMIT": "Max search results per query",
    "TELEGRAM_BOT_TOKEN": "Telegram bot token for notifications",
    "TELEGRAM_WEBHOOK_URL": "Telegram webhook callback URL",
    "TELEGRAM_WEBHOOK_SECRET": "Secret token for Telegram webhook auth",
    "TELEGRAM_ALLOWED_USERS": "Comma-separated allowed Telegram user IDs",
    "NOTIFY_CHANNEL": "Notification channel (telegram or none)",
    "NOTIFY_DEADLINE_DAYS": "Days before deadline to send reminders",
    "NOTIFY_DIGEST_DAY": "Day of week for weekly digest",
    "NOTIFY_DIGEST_TIME": "Time of day for weekly digest (HH:MM)",
    "NOTIFY_STALE_DAYS": "Days of inactivity before stale warning",
    "AUTO_ARCHIVE_DAYS": "Days of inactivity before auto-archive",
    "RECLASSIFY_INTERVAL_HOURS": "Hours between auto-reclassification runs",
    "RECLASSIFY_CONFIDENCE_THRESHOLD": "Min confidence to auto-accept classification",
    "WEB_PUBLIC_URL": "Public-facing URL of the PARA instance",
    "BACKUP_CLOUD_ENABLED": "Enable S3-compatible cloud backup",
    "BACKUP_CLOUD_ENDPOINT": "S3-compatible endpoint URL",
    "BACKUP_CLOUD_BUCKET": "S3 bucket name for backups",
    "BACKUP_CLOUD_ACCESS_KEY": "S3 access key",
    "BACKUP_CLOUD_SECRET_KEY": "S3 secret key",
    "BACKUP_CLOUD_RETENTION_DAYS": "Days to retain cloud backups",
    "EVENT_WEBHOOK_URL": "Webhook URL for event bus dispatch",
    "EVENT_WEBHOOK_SECRET": "Secret for event webhook auth",
    "EVENT_TYPES_ENABLED": "Comma-separated enabled event types",
    "EVENT_DISPATCH_RETRIES": "Max retries for event dispatch",
    "TASK_AUTO_EXTRACT": "Auto-extract action items from notes",
    "TASK_ACTION_VERBS": "Comma-separated Thai action verbs for task detection",
    "AUTONOMY_LEVEL": "Autonomy level (suggest_only, auto, etc.)",
    "AUTONOMY_DAILY_HOUR": "Hour of day for autonomous task generation",
}

# ── Sensitive field patterns ────────────────────────────────────────────────

SENSITIVE_PATTERNS: list[str] = [
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
]


def is_sensitive_key(key: str) -> bool:
    """Return True if the setting name suggests it contains sensitive data."""
    upper = key.upper()
    return any(p in upper for p in SENSITIVE_PATTERNS)


def mask_value(key: str, value: Any) -> str:
    """Mask sensitive values, showing only the first 4 characters."""
    if not is_sensitive_key(key):
        return str(value)
    s = str(value)
    if len(s) <= 4:
        return s + "****"
    return s[:4] + "****"


def get_setting_description(key: str) -> str:
    """Return a human-readable description for a setting key."""
    return SETTING_DESCRIPTIONS.get(key, "")


def get_env_settings_groups(settings_obj: Any) -> dict[str, list[dict]]:
    """Build grouped env settings with values, descriptions, and mask status.

    Args:
        settings_obj: The ``app.config.settings`` Settings instance.

    Returns:
        A dict mapping group names to lists of setting dicts::

            {
                "Core": [
                    {"key": "PARA_PORT", "value": "8731",
                     "description": "Port the FastAPI app listens on",
                     "sensitive": False, "masked": False},
                    ...
                ],
                ...
            }
    """
    groups: dict[str, list[dict]] = {}
    seen: set[str] = set()

    for group_name, keys in SETTING_GROUPS.items():
        items: list[dict] = []
        for key in keys:
            if key in seen:
                continue
            seen.add(key)
            value = getattr(settings_obj, key, None)
            if value is None:
                value = ""
            sensitive = is_sensitive_key(key)
            items.append({
                "key": key,
                "value": mask_value(key, value) if sensitive else str(value),
                "raw_value": str(value),
                "description": get_setting_description(key),
                "sensitive": sensitive,
                "masked": sensitive,
            })
        if items:
            groups[group_name] = items

    # Catch any settings not in the group mapping
    all_keys = list(settings_obj.model_fields.keys())
    for key in all_keys:
        if key not in seen:
            seen.add(key)
            value = getattr(settings_obj, key, None)
            if value is None:
                value = ""
            sensitive = is_sensitive_key(key)
            groups.setdefault("Other", []).append({
                "key": key,
                "value": mask_value(key, value) if sensitive else str(value),
                "raw_value": str(value),
                "description": get_setting_description(key),
                "sensitive": sensitive,
                "masked": sensitive,
            })

    return groups
