"""/api/settings — read and update runtime settings stored in the settings table."""

import logging
import re

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from app.config import settings
from app.database import get_db
from app.routes.notes import require_api_key
from app.scheduler import digest_trigger, reclassify_trigger, scheduler

router = APIRouter(prefix="/api", tags=["settings"])
logger = logging.getLogger("para.routes.settings")

SETTINGS_KEYS: dict[str, type] = {
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
}

_DAY_OF_WEEK_RE = re.compile(
    r"^(mon|tue|wed|thu|fri|sat|sun)(-(mon|tue|wed|thu|fri|sat|sun))?"
    r"(,(mon|tue|wed|thu|fri|sat|sun)(-(mon|tue|wed|thu|fri|sat|sun))?)*$",
    re.IGNORECASE,
)
_DIGEST_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _validate_setting(key: str, value) -> None:
    """Reject values that would silently break scheduler cron triggers."""
    if key == "NOTIFY_DEADLINE_DAYS":
        parts = [p.strip() for p in str(value).split(",")]
        if not parts or not all(p.isdigit() for p in parts):
            raise HTTPException(
                status_code=422,
                detail="NOTIFY_DEADLINE_DAYS must be a comma-separated list of non-negative integers",
            )
    elif key == "NOTIFY_DIGEST_DAY":
        if not _DAY_OF_WEEK_RE.match(str(value)):
            raise HTTPException(
                status_code=422,
                detail="NOTIFY_DIGEST_DAY must be a valid day-of-week expression, e.g. 'mon'",
            )
    elif key == "NOTIFY_DIGEST_TIME":
        if not _DIGEST_TIME_RE.match(str(value)):
            raise HTTPException(status_code=422, detail="NOTIFY_DIGEST_TIME must be in HH:MM format")
    elif key in ("NOTIFY_STALE_DAYS", "AUTO_ARCHIVE_DAYS", "RECLASSIFY_INTERVAL_HOURS", "CHAT_HISTORY_MAX") and value <= 0:
        raise HTTPException(status_code=422, detail=f"{key} must be a positive integer")
    elif key == "RECLASSIFY_CONFIDENCE_THRESHOLD" and not 0.0 <= value <= 1.0:
        raise HTTPException(status_code=422, detail="RECLASSIFY_CONFIDENCE_THRESHOLD must be between 0.0 and 1.0")


async def get_settings_dict(db: aiosqlite.Connection) -> dict:
    result: dict = {}
    for key, cast in SETTINGS_KEYS.items():
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        if row is not None:
            try:
                result[key] = cast(row["value"])
            except (ValueError, TypeError):
                result[key] = getattr(settings, key)
        else:
            result[key] = getattr(settings, key)
    return result


@router.get("/settings")
async def get_settings(db: aiosqlite.Connection = Depends(get_db)):
    return await get_settings_dict(db)


@router.put("/settings", dependencies=[Depends(require_api_key)])
async def update_settings(payload: dict, db: aiosqlite.Connection = Depends(get_db)):
    updated: dict = {}
    for key, value in payload.items():
        if key not in SETTINGS_KEYS:
            raise HTTPException(status_code=422, detail=f"Unknown setting: {key}")
        cast = SETTINGS_KEYS[key]
        try:
            cast_value = cast(value)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail=f"Invalid value for {key}")
        _validate_setting(key, cast_value)
        await db.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, str(cast_value)),
        )
        updated[key] = cast_value
    await db.commit()

    # Apply changes to the live settings object so scheduler jobs (which read
    # settings.* at call time) reflect the update without a process restart.
    for key, cast_value in updated.items():
        setattr(settings, key, cast_value)

    # RECLASSIFY_INTERVAL_HOURS and NOTIFY_DIGEST_DAY/TIME are baked into cron
    # triggers when the jobs are registered, so those two jobs need re-scheduling
    # for the change to actually take effect.
    if "RECLASSIFY_INTERVAL_HOURS" in updated:
        try:
            scheduler.reschedule_job("reclassify", trigger=reclassify_trigger())
        except Exception:
            logger.exception("Failed to reschedule reclassify job after settings update")
    if "NOTIFY_DIGEST_DAY" in updated or "NOTIFY_DIGEST_TIME" in updated:
        try:
            scheduler.reschedule_job("weekly_digest", trigger=digest_trigger())
        except Exception:
            logger.exception("Failed to reschedule weekly_digest job after settings update")

    return updated
