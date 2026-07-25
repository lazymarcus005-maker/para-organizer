"""/api/backup/* — create, list, restore, delete and download database backups."""

import logging
from datetime import datetime
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.config import settings
from app.database import get_db
from app.routes.notes import require_api_key

router = APIRouter(prefix="/api", tags=["backup"])
logger = logging.getLogger("para.routes.backup")


def backup_dir() -> Path:
    return Path(settings.PARA_DB_PATH).parent / "backups"


def safe_backup_path(filename: str) -> Path:
    name = Path(filename).name
    if name != filename or not name.endswith(".db") or name in ("", ".", ".."):
        raise HTTPException(status_code=400, detail="Invalid backup filename")
    return backup_dir() / name


async def create_backup_file(db: aiosqlite.Connection) -> dict:
    try:
        directory = backup_dir()
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.exception("Failed to create backup directory")
        raise HTTPException(status_code=500, detail=f"Could not create backup directory: {e}")

    filename = f"para_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    dest = directory / filename
    try:
        target = await aiosqlite.connect(str(dest))
        try:
            await db.backup(target)
        finally:
            await target.close()
        stat = dest.stat()
    except (aiosqlite.Error, OSError) as e:
        logger.exception("Backup creation failed")
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Backup failed: {e}")

    return {
        "filename": filename,
        "size": stat.st_size,
        "date": datetime.fromtimestamp(stat.st_mtime).isoformat(),
    }


def list_backup_files() -> list[dict]:
    directory = backup_dir()
    if not directory.exists():
        return []
    try:
        paths = sorted(directory.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [
            {
                "filename": path.name,
                "size": (stat := path.stat()).st_size,
                "date": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
            for path in paths
        ]
    except OSError:
        logger.exception("Failed to list backup directory %s", directory)
        return []


async def restore_backup_file(filename: str, db: aiosqlite.Connection) -> dict:
    source_path = safe_backup_path(filename)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Backup not found")
    try:
        source = await aiosqlite.connect(str(source_path))
        try:
            await source.backup(db)
        finally:
            await source.close()
        await db.commit()
    except (aiosqlite.Error, OSError) as e:
        logger.exception("Backup restore failed for %s", filename)
        raise HTTPException(status_code=500, detail=f"Restore failed: {e}")
    return {"restored": filename}


def delete_backup_file(filename: str) -> dict:
    path = safe_backup_path(filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Backup not found")
    try:
        path.unlink()
    except OSError as e:
        logger.exception("Failed to delete backup %s", filename)
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")
    return {"deleted": filename}


@router.post("/backup", dependencies=[Depends(require_api_key)])
async def create_backup(db: aiosqlite.Connection = Depends(get_db)):
    return await create_backup_file(db)


@router.get("/backup", dependencies=[Depends(require_api_key)])
async def list_backups():
    return {"backups": list_backup_files()}


@router.post("/backup/restore/{filename}", dependencies=[Depends(require_api_key)])
async def restore_backup(filename: str, db: aiosqlite.Connection = Depends(get_db)):
    return await restore_backup_file(filename, db)


@router.delete("/backup/{filename}", dependencies=[Depends(require_api_key)])
async def delete_backup(filename: str):
    return delete_backup_file(filename)


@router.get("/backup/download/{filename}", dependencies=[Depends(require_api_key)])
async def download_backup(filename: str):
    path = safe_backup_path(filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(path, media_type="application/octet-stream", filename=filename)
