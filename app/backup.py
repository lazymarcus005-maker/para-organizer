"""Cloud backup — pg_dump + S3-compatible upload.

This module handles periodic backups of the PostgreSQL database to
S3-compatible object storage (MinIO, AWS S3, Backblaze B2, etc.).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

import httpx

from app.config import settings

logger = logging.getLogger("para.backup")


async def cloud_backup() -> bool:
    """Dump PostgreSQL and upload to S3-compatible storage.

    Returns:
        True if the backup was created and uploaded successfully, False otherwise.
    """
    if not settings.BACKUP_CLOUD_ENABLED:
        logger.info("Cloud backup disabled — skipping")
        return False

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"para_backup_{timestamp}.sql.gz"
    local_path = f"/tmp/{filename}"

    # ── pg_dump ──────────────────────────────────────────────────────────────
    logger.info("Starting pg_dump...")
    try:
        # Extract connection parameters from the async URL
        dsn = settings.PARA_DB_URL.replace("postgresql+asyncpg://", "postgresql://")
        cmd = f"pg_dump {dsn} | gzip > {local_path}"
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error("pg_dump failed (exit %d): %s", proc.returncode, stderr.decode()[:500])
            return False

        if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
            logger.error("pg_dump produced empty file")
            return False

        file_size = os.path.getsize(local_path)
        logger.info("pg_dump complete: %s (%.1f MB)", filename, file_size / (1024 * 1024))
    except Exception as e:
        logger.exception("pg_dump failed with exception: %s", e)
        return False

    # ── Upload to S3 ─────────────────────────────────────────────────────────
    logger.info("Uploading to S3: %s/%s", settings.BACKUP_CLOUD_BUCKET, filename)
    try:
        endpoint = settings.BACKUP_CLOUD_ENDPOINT.rstrip("/")
        bucket = settings.BACKUP_CLOUD_BUCKET
        upload_url = f"{endpoint}/{bucket}/{filename}"

        async with httpx.AsyncClient(timeout=300.0) as client:
            with open(local_path, "rb") as f:
                resp = await client.put(
                    upload_url,
                    content=f.read(),
                    headers={
                        "Authorization": f"Bearer {settings.BACKUP_CLOUD_ACCESS_KEY}",
                        "Content-Type": "application/gzip",
                    },
                )
                resp.raise_for_status()

        logger.info("Backup uploaded successfully: %s", filename)
        return True
    except httpx.HTTPError as e:
        logger.error("S3 upload failed: %s", e)
        return False
    except Exception as e:
        logger.exception("S3 upload failed with exception: %s", e)
        return False
    finally:
        # Clean up local file
        try:
            os.remove(local_path)
        except OSError:
            pass
