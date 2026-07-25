"""Tests for backup/restore, JSON import/export download, and settings API."""

import io
import json
import zipfile
from pathlib import Path

import pytest

from app.config import settings
from app.database import get_connection
from tests.conftest import insert_note

AUTH = {"Authorization": "Bearer cron-secret"}


# ─────────────────────────── Backup ───────────────────────────

def test_backup_requires_auth(client):
    assert client.post("/api/backup").status_code == 401
    assert client.get("/api/backup").status_code == 401


def test_backup_create_and_list(client, test_db):
    resp = client.post("/api/backup", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["filename"].endswith(".db")
    assert data["size"] > 0
    assert data["date"]

    backup_path = Path(settings.PARA_DB_PATH).parent / "backups" / data["filename"]
    assert backup_path.exists()

    listing = client.get("/api/backup", headers=AUTH).json()
    assert any(b["filename"] == data["filename"] for b in listing["backups"])


def test_backup_restore(client, test_db):
    note_id = await_sync(insert_note(title="Original", content="before backup"))

    created = client.post("/api/backup", headers=AUTH).json()

    delete_resp = client.delete(f"/api/notes/{note_id}")
    assert delete_resp.status_code == 200
    async_count = await_sync(_count_notes())
    assert async_count == 0

    restore_resp = client.post(f"/api/backup/restore/{created['filename']}", headers=AUTH)
    assert restore_resp.status_code == 200

    assert await_sync(_count_notes()) == 1
    row = await_sync(_first_note())
    assert row["title"] == "Original"


def test_backup_delete(client, test_db):
    created = client.post("/api/backup", headers=AUTH).json()
    filename = created["filename"]

    del_resp = client.delete(f"/api/backup/{filename}", headers=AUTH)
    assert del_resp.status_code == 200

    listing = client.get("/api/backup", headers=AUTH).json()
    assert all(b["filename"] != filename for b in listing["backups"])

    assert client.delete(f"/api/backup/{filename}", headers=AUTH).status_code == 404


def test_backup_download(client, test_db):
    created = client.post("/api/backup", headers=AUTH).json()
    resp = client.get(f"/api/backup/download/{created['filename']}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert created["filename"] in resp.headers.get("content-disposition", "")


def test_backup_rejects_path_traversal(client, test_db):
    assert client.get("/api/backup/download/..%2Fpara.db", headers=AUTH).status_code in (400, 404)
    assert client.delete("/api/backup/..%2Fpara.db", headers=AUTH).status_code in (400, 404)


def test_safe_backup_path_validation(test_db):
    from fastapi import HTTPException
    from app.routes.backup import safe_backup_path

    with pytest.raises(HTTPException):
        safe_backup_path("../para.db")
    with pytest.raises(HTTPException):
        safe_backup_path("notes.txt")
    assert safe_backup_path("para_backup_20260101_000000.db").name == "para_backup_20260101_000000.db"


# ─────────────────────────── Import ───────────────────────────

def test_import_requires_auth(client):
    files = {"file": ("notes.json", b"[]", "application/json")}
    assert client.post("/api/import", files=files).status_code == 401


def test_import_valid_json(client, test_db):
    notes = [
        {"title": "โน้ตไทย", "content": "เนื้อหา", "para_category": "projects",
         "priority": "high", "tags": ["ทดสอบ"], "source": "manual"},
        {"title": "Plain", "content": "body"},
    ]
    files = {"file": ("notes.json", json.dumps(notes, ensure_ascii=False).encode("utf-8"), "application/json")}
    resp = client.post("/api/import", files=files, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["imported"] == 2
    assert body["skipped"] == 0
    assert await_sync(_count_notes()) == 2


def test_import_skips_invalid(client, test_db):
    payload = [
        {"title": "Valid", "content": "ok"},
        {"content": "missing title"},
        "not-an-object",
        {"title": "", "content": "empty title"},
    ]
    files = {"file": ("notes.json", json.dumps(payload).encode("utf-8"), "application/json")}
    resp = client.post("/api/import", files=files, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["imported"] == 1
    assert body["skipped"] == 3
    assert len(body["errors"]) == 3


def test_import_invalid_json_file(client, test_db):
    files = {"file": ("bad.json", b"this is not json", "application/json")}
    assert client.post("/api/import", files=files, headers=AUTH).status_code == 400


def test_import_non_array_json(client, test_db):
    files = {"file": ("obj.json", b'{"a": 1}', "application/json")}
    assert client.post("/api/import", files=files, headers=AUTH).status_code == 400


# ─────────────────────────── Export download ───────────────────────────

def test_export_download_json(client, test_db):
    await_sync(insert_note(title="Export me"))
    resp = client.get("/api/export/download")
    assert resp.status_code == 200
    assert "para-export.json" in resp.headers.get("content-disposition", "")
    data = resp.json()
    assert isinstance(data, list)
    assert any(n["title"] == "Export me" for n in data)


def test_export_download_markdown_zip(client, test_db):
    await_sync(insert_note(title="Md note"))
    resp = client.get("/api/export/download?format=md")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert any(name.endswith(".md") for name in zf.namelist())


# ─────────────────────────── Settings ───────────────────────────

def test_settings_get_defaults(client, test_db):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["AUTO_ARCHIVE_DAYS"] == 30
    assert body["NOTIFY_DEADLINE_DAYS"] == "7,3,1"
    assert body["RECLASSIFY_CONFIDENCE_THRESHOLD"] == 0.7
    assert set(body.keys()) == {
        "NOTIFY_DEADLINE_DAYS", "NOTIFY_DIGEST_DAY", "NOTIFY_DIGEST_TIME",
        "NOTIFY_STALE_DAYS", "AUTO_ARCHIVE_DAYS",
        "RECLASSIFY_INTERVAL_HOURS", "RECLASSIFY_CONFIDENCE_THRESHOLD",
    }


def test_settings_update_and_persist(client, test_db):
    put_resp = client.put("/api/settings", json={"AUTO_ARCHIVE_DAYS": 45, "NOTIFY_STALE_DAYS": 21}, headers=AUTH)
    assert put_resp.status_code == 200
    assert put_resp.json() == {"AUTO_ARCHIVE_DAYS": 45, "NOTIFY_STALE_DAYS": 21}

    body = client.get("/api/settings").json()
    assert body["AUTO_ARCHIVE_DAYS"] == 45
    assert body["NOTIFY_STALE_DAYS"] == 21


def test_settings_update_requires_auth(client, test_db):
    assert client.put("/api/settings", json={"AUTO_ARCHIVE_DAYS": 10}).status_code == 401


def test_settings_update_rejects_unknown_key(client, test_db):
    assert client.put("/api/settings", json={"BOGUS_KEY": 1}, headers=AUTH).status_code == 422


def test_settings_update_rejects_bad_type(client, test_db):
    resp = client.put("/api/settings", json={"AUTO_ARCHIVE_DAYS": "not-a-number"}, headers=AUTH)
    assert resp.status_code == 422


# ─────────────────────────── Settings page ───────────────────────────

def test_settings_page_renders(client, test_db):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Settings" in resp.text
    assert "Backups" in resp.text


# ─────────────────────────── async helpers ───────────────────────────

def await_sync(coro):
    """Run a coroutine to completion on a fresh event loop (test DB is file-backed)."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _count_notes() -> int:
    async with get_connection() as db:
        row = await (await db.execute("SELECT COUNT(*) c FROM notes")).fetchone()
        return row["c"]


async def _first_note():
    async with get_connection() as db:
        return await (await db.execute("SELECT * FROM notes ORDER BY id LIMIT 1")).fetchone()
