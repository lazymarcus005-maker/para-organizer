"""/api/backup/* — create, list, restore, delete and download database backups."""

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import aiosqlite
import httpx
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

    cloud_uploaded = await upload_to_cloud(dest)

    return {
        "filename": filename,
        "size": stat.st_size,
        "date": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "cloud_uploaded": cloud_uploaded,
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


def _s3_sign(method: str, url: str, headers: dict, payload_hash: str) -> dict:
    """Minimal AWS Signature V4 signer for S3-compatible PUT requests."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.netloc
    path = quote(parsed.path or "/")
    region = "us-east-1"
    service = "s3"
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    headers["x-amz-date"] = amz_date
    headers["x-amz-content-sha256"] = payload_hash
    headers["host"] = host

    signed_header_keys = sorted(headers.keys())
    signed_headers_str = ";".join(signed_header_keys)
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in signed_header_keys)

    canonical_request = f"{method}\n{path}\n\n{canonical_headers}\n{signed_headers_str}\n{payload_hash}"
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
        + hashlib.sha256(canonical_request.encode()).hexdigest()
    )

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _hmac(("AWS4" + settings.BACKUP_CLOUD_SECRET_KEY).encode(), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={settings.BACKUP_CLOUD_ACCESS_KEY}/{credential_scope}, "
        f"SignedHeaders={signed_headers_str}, Signature={signature}"
    )
    return headers


async def upload_to_cloud(file_path: Path) -> bool:
    """Upload a backup file to S3-compatible object storage. Best-effort."""
    if not settings.BACKUP_CLOUD_ENABLED or not settings.BACKUP_CLOUD_ENDPOINT:
        return False
    url = f"{settings.BACKUP_CLOUD_ENDPOINT.rstrip('/')}/{settings.BACKUP_CLOUD_BUCKET}/{file_path.name}"
    data = file_path.read_bytes()
    payload_hash = hashlib.sha256(data).hexdigest()
    headers = {"content-type": "application/octet-stream"}
    try:
        signed = _s3_sign("PUT", url, headers, payload_hash)
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.put(url, content=data, headers=signed)
            resp.raise_for_status()
        logger.info("Uploaded backup %s to cloud", file_path.name)
        return True
    except Exception:
        logger.exception("Cloud upload failed for %s", file_path.name)
        return False


async def cleanup_old_cloud_backups() -> int:
    """Delete cloud backups older than BACKUP_CLOUD_RETENTION_DAYS. Best-effort."""
    if not settings.BACKUP_CLOUD_ENABLED or not settings.BACKUP_CLOUD_ENDPOINT:
        return 0
    deleted = 0
    try:
        base_url = settings.BACKUP_CLOUD_ENDPOINT.rstrip("/")
        list_url = f"{base_url}/{settings.BACKUP_CLOUD_BUCKET}?list-type=2&prefix=para_backup_"
        headers: dict = {}
        signed = _s3_sign("GET", list_url, headers, hashlib.sha256(b"").hexdigest())
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(list_url, headers=signed)
            resp.raise_for_status()
            import xml.etree.ElementTree as ET
            root = ET.fromstring(resp.text)
            ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
            now = datetime.now(timezone.utc)
            for content in root.findall(".//s3:Contents", ns):
                key_el = content.find("s3:Key", ns)
                date_el = content.find("s3:LastModified", ns)
                if key_el is None or date_el is None:
                    continue
                last_modified = datetime.fromisoformat(date_el.text.replace("Z", "+00:00"))
                age_days = (now - last_modified).days
                if age_days > settings.BACKUP_CLOUD_RETENTION_DAYS:
                    del_url = f"{base_url}/{settings.BACKUP_CLOUD_BUCKET}/{key_el.text}"
                    del_headers: dict = {}
                    del_signed = _s3_sign("DELETE", del_url, del_headers, hashlib.sha256(b"").hexdigest())
                    del_resp = await client.delete(del_url, headers=del_signed)
                    if del_resp.status_code < 300:
                        deleted += 1
        if deleted:
            logger.info("Cleaned up %d old cloud backups", deleted)
    except Exception:
        logger.exception("Cloud backup cleanup failed")
    return deleted


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
