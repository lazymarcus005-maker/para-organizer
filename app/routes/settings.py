"""/api/settings — read and update runtime settings stored in the settings table."""

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from app.config import settings
from app.database import get_db
from app.routes.notes import require_api_key

router = APIRouter(prefix="/api", tags=["settings"])

SETTINGS_KEYS: dict[str, type] = {
    "NOTIFY_DEADLINE_DAYS": str,
    "NOTIFY_DIGEST_DAY": str,
    "NOTIFY_DIGEST_TIME": str,
    "NOTIFY_STALE_DAYS": int,
    "AUTO_ARCHIVE_DAYS": int,
    "RECLASSIFY_INTERVAL_HOURS": int,
    "RECLASSIFY_CONFIDENCE_THRESHOLD": float,
}


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
        await db.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, str(cast_value)),
        )
        updated[key] = cast_value
    await db.commit()
    return updated
