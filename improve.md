# PARA Organizer v5 — Upgrade Plan

> Branch: `features/upgrade` · Target: Production-ready distributed deployment
> Current: Single container (SQLite) → Target: Multi-service (PostgreSQL + Redis + Worker + Scheduler)

## 🚫 HARD RULE: ห้าม Merge `features/upgrade` เข้า `main` เด็ดขาด

**เหตุผล:** branch นี้ใช้สำหรับ development + data migration + testing เท่านั้น
- `main` คือ production branch ที่รันอยู่จริง (SQLite, single container)
- `features/upgrade` คือ experimental branch สำหรับ PostgreSQL + Redis + Worker stack
- **ห้าม merge ไม่ว่ากรณีใด ๆ** จนกว่าจะมีการตัดสินใจใหม่

**มาตรการป้องกัน:**
1. GitHub branch protection — ตั้ง `main` เป็น protected branch (ต้องมี PR review + status checks)
2. Git hook — ป้องกันการ merge จาก branch นี้
3. ไฟล์ `AGENTS.md` — บันทึกกฎนี้ให้ AI agents ทราบ

---

## Phase 0: Pre-Flight — Fix Known Bugs First

### 0.1 Fix Scheduler Topic Names

**Bug found:** `scheduler_service.py` pushes topics `archive` and `autonomy` that **don't exist** in `task_queue.TOPICS` or `worker.HANDLERS`. These jobs fail silently every day.

**File:** `app/scheduler_service.py`

| Line | Current Topic | Should Be | Reason |
|---|---|---|---|
| 113 | `"archive"` | `"classify"` | Auto-archive should trigger re-classify |
| 176 | `"autonomy"` | `"escalate"` | Autonomous task gen → escalate priorities |

**Fix:**

```python
# Line 113: change
args=["archive", {}],
# to
args=["classify", {"source": "auto_archive"}],

# Line 176: change
args=["autonomy", {}],
# to
args=["escalate", {"source": "autonomy"}],
```

### 0.2 Verify `scripts/migrate_pg.py` Schema Alignment

**Existing file:** `scripts/migrate_pg.py` (223 lines) — well-written, uses asyncpg + aiosqlite, batched INSERT, row count verification.

**Tables it migrates:** notes, links, history, notifications, settings, chat_messages, llm_usage, events, tasks, items, feedback

**Schema check needed:** The SQLite DB lives in a Docker volume (`para-organizer_para-data`). Before running migration, verify that the actual SQLite schema matches what `migrate_pg.py` expects:

```bash
# Copy migration script into container and run schema check
sg docker -c "docker cp scripts/migrate_pg.py para-organizer-app:/tmp/migrate_pg.py"
sg docker -c "docker exec para-organizer-app python3 -c \"
import sqlite3
conn = sqlite3.connect('/var/lib/para-organizer/data/para.db')
tables = conn.execute(\\\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\\\").fetchall()
for (tname,) in tables:
    cols = conn.execute(f'PRAGMA table_info({tname})').fetchall()
    print(f'{tname}: {[c[1] for c in cols]}')
conn.close()
\""
```

Compare output with the `TABLES` list in `scripts/migrate_pg.py` (lines 85-121). If any column is missing or extra, update the script.

---

## Phase 1: Fix Infrastructure

### 1.1 Fix docker-compose.yml

**Problem:** `container_name` + `deploy.replicas` conflict — Docker Compose rejects the file.

**Fix:** Remove `container_name` from all services that have `replicas > 1` (para-app, para-worker, para-mcp). Keep `container_name` only on singletons (para-db, para-redis, para-scheduler, para-backup, para-traefik).

**File:** `docker-compose.yml`

```yaml
# Change this (para-app):
services:
  para-app:
    # container_name: para-app          ← REMOVE THIS
    deploy:
      replicas: 2
```

Also remove the obsolete `version: '3.8'` line at the top.

### 1.2 Create .env Template

**File:** `.env.example`

```bash
# ── Core ──
PARA_PORT=8731
PARA_DB_URL=postgresql+asyncpg://para:password@para-db:5432/para
PARA_REDIS_URL=redis://para-redis:6379/0
PARA_DB_POOL_SIZE=10
PARA_DB_MAX_OVERFLOW=20
PARA_REDIS_CACHE_TTL=60
PARA_SECRET_KEY=change-me-in-production

# ── LLM ──
OLLAMA_API_KEY=
OLLAMA_BASE_URL=https://ollama.com/v1
LLM_PRIMARY=deepseek-v4-flash
LLM_FALLBACK=gpt-oss:20b
LLM_TIMEOUT=60
LLM_MAX_RETRIES=2

# ── Telegram ──
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_URL=
TELEGRAM_ALLOWED_USERS=8722556718

# ── Notification ──
NOTIFY_CHANNEL=telegram
NOTIFY_DEADLINE_DAYS=7,3,1
NOTIFY_DIGEST_DAY=mon
NOTIFY_DIGEST_TIME=08:00
NOTIFY_STALE_DAYS=14

# ── Auto ──
AUTO_ARCHIVE_DAYS=30
RECLASSIFY_INTERVAL_HOURS=6
RECLASSIFY_CONFIDENCE_THRESHOLD=0.7

# ── Web ──
WEB_PUBLIC_URL=https://para.mxlabs.cloud

# ── Embeddings ──
EMBED_PROVIDER=ollama_local
EMBED_BASE_URL=http://host.docker.internal:11434
EMBED_MODEL=nomic-embed-text
RAG_HYBRID_ENABLED=true
RAG_HYBRID_RATIO=0.5

# ── Cloud Backup ──
BACKUP_CLOUD_ENABLED=false
BACKUP_CLOUD_ENDPOINT=
BACKUP_CLOUD_BUCKET=
BACKUP_CLOUD_ACCESS_KEY=
BACKUP_CLOUD_SECRET_KEY=
BACKUP_CLOUD_RETENTION_DAYS=30
```

Then copy to `.env` and fill in real values.

### 1.3 Fix Dockerfile

**Problem:** HEALTHCHECK uses `httpx.get('http://localhost:8731/api/stats')` — `/api/stats` may be slow or fail on empty DB.

**Fix:** Change HEALTHCHECK to use `/api/health/live` (lightweight endpoint):

```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python3 -c "import httpx; r=httpx.get('http://localhost:8731/api/health/live', timeout=5); exit(0 if r.status_code==200 else 1)" || exit 1
```

### 1.4 Fix Startup Ordering

**Problem:** Services depend on `para-db` and `para-redis` but there's no wait-for-it script.

**Fix:** Add a `wait-for-it.sh` script and use it as entrypoint wrapper:

**File:** `scripts/wait-for-it.sh`

```bash
#!/bin/bash
# Wait for a TCP host:port to be available
HOST="$1"
PORT="$2"
shift 2
until nc -z "$HOST" "$PORT" 2>/dev/null; do
  echo "Waiting for $HOST:$PORT..."
  sleep 2
done
exec "$@"
```

Then in docker-compose.yml, override the command for para-app:

```yaml
para-app:
  command: >
    sh -c "
      ./scripts/wait-for-it.sh para-db 5432 &&
      ./scripts/wait-for-it.sh para-redis 6379 &&
      python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8731
    "
```

### 1.5 Dependencies Check

**requirements.txt** already has all needed deps ✅:
- `redis[hiredis]>=5.0.0` ✅
- `asyncpg>=0.29.0` ✅
- `sqlalchemy[asyncio]>=2.0` ✅
- `alembic>=1.13.0` ✅
- `pgvector>=0.2.0` ✅
- `apscheduler>=3.10.0` ✅

---

## Phase 2: Fill Worker Logic

### ⚠️ Key Finding: Most Logic Already Exists

**7 of 8 handlers already have real implementations** in sqlite-based modules. They just need to be **ported** from `app.database` (aiosqlite) → `app.database_v2` (async SQLAlchemy + PostgreSQL).

| Handler | Existing Module | Lines | What It Does |
|---|---|---|---|
| `handle_classify` | `app/classifier.py` | 212 | LLM PARA classification via Ollama Cloud, confidence routing, feedback integration |
| `handle_embed` | `app/embed.py` | 70 | Embedding via Ollama (local/cloud), OpenAI-compatible endpoint |
| `handle_notify` | `app/notifier.py` | 177 | Telegram send, deadline/stale/digest formatting, inline keyboards |
| `handle_link` | `app/linker.py` | 126 | Semantic auto-linking via sqlite-vec, similarity threshold |
| `handle_distill` | `app/distill.py` | 58 | 1-line summary generation via LLM for archived notes |
| `handle_review` | `app/review.py` | 237 | Weekly AI review: gather data, LLM digest, fallback deterministic summary |
| `handle_escalate` | `app/scheduler.py` | 459 | Reclassify low-confidence, deadline escalation, auto-archive, autonomous tasks |
| `handle_backup` | — | 0 | **Only one without prior implementation** — needs to be written from scratch |

### 2.1 Porting Strategy

For each existing module, the port is:

1. Replace `from app.database import get_connection` → `from app.database_v2 import async_session_factory`
2. Replace `aiosqlite` cursor calls → SQLAlchemy async session queries
3. Replace `row_to_note()` → `Note` ORM model
4. Replace `sqlite-vec` → `pgvector` (via `app/vector_store.py` which already supports both)

**Example — handle_classify in worker.py:**

```python
async def handle_classify(payload: dict[str, Any]) -> None:
    note_id = payload.get("note_id")
    logger.info("Classify note %s", note_id)
    from app.classifier import classify_note
    from app.database_v2 import async_session_factory
    from app.models_v2 import Note
    from sqlalchemy import select

    async with async_session_factory() as session:
        result = await session.execute(select(Note).where(Note.id == note_id))
        note = result.scalar_one_or_none()
        if note is None:
            logger.warning("Note %s not found", note_id)
            return

        classification = await classify_note(note.content, note.title)
        note.para_category = classification.get("para_category", note.para_category)
        note.priority = classification.get("priority", note.priority)
        note.tags = classification.get("tags", note.tags)
        note.llm_confidence = classification.get("confidence", 0.0)
        note.llm_reasoning = classification.get("reasoning", "")
        await session.commit()
```

### 2.2 handle_backup (Only New Module)

**File:** `app/backup.py` (new)

```python
"""Cloud backup — pg_dump + S3-compatible upload."""
import asyncio
import logging
import subprocess
from datetime import datetime

import httpx

from app.config import settings

logger = logging.getLogger("para.backup")

async def cloud_backup() -> bool:
    """Dump PostgreSQL and upload to S3-compatible storage."""
    if not settings.BACKUP_CLOUD_ENABLED:
        logger.info("Cloud backup disabled — skipping")
        return False

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"para_backup_{timestamp}.sql.gz"

    # pg_dump
    cmd = f"pg_dump {settings.PARA_DB_URL} | gzip > /tmp/{filename}"
    proc = await asyncio.create_subprocess_shell(cmd)
    await proc.wait()

    if proc.returncode != 0:
        logger.error("pg_dump failed")
        return False

    # Upload to S3
    async with httpx.AsyncClient() as client:
        with open(f"/tmp/{filename}", "rb") as f:
            resp = await client.put(
                f"{settings.BACKUP_CLOUD_ENDPOINT}/{settings.BACKUP_CLOUD_BUCKET}/{filename}",
                content=f.read(),
                headers={
                    "Authorization": f"Bearer {settings.BACKUP_CLOUD_ACCESS_KEY}",
                },
            )
            resp.raise_for_status()

    logger.info("Backup uploaded: %s", filename)
    return True
```

### 2.3 Error Handling & Retry

Add retry decorator to all handlers:

```python
import asyncio
from functools import wraps

def retry(max_attempts=3, delay=2):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    logger.warning(f"Handler failed (attempt {attempt+1}): {e}")
                    await asyncio.sleep(delay * (2 ** attempt))
            return None
        return wrapper
    return decorator
```

---

## Phase 3: Data Migration

### 3.1 Migration Script

**Existing file:** `scripts/migrate_pg.py` (223 lines) ✅

This script already exists and is well-structured:
- Uses `aiosqlite` to read from SQLite
- Uses `asyncpg` to write to PostgreSQL
- Batched INSERT (500 rows at a time)
- Row count verification after each table
- Handles JSON transforms (tags, source_metadata, recurrence, payload)
- Skips embeddings (recommends backfill job instead)
- Skips FTS5 (PostgreSQL tsvector is auto-generated)

**What needs to be verified before running:**

```bash
# 1. Check SQLite schema matches script expectations
sg docker -c "docker exec para-organizer-app python3 -c \"
import sqlite3
conn = sqlite3.connect('/var/lib/para-organizer/data/para.db')
tables = conn.execute(\\\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\\\").fetchall()
for (tname,) in tables:
    cols = conn.execute(f'PRAGMA table_info({tname})').fetchall()
    print(f'{tname}: {[c[1] for c in cols]}')
conn.close()
\""

# 2. Compare with TABLES list in scripts/migrate_pg.py (lines 85-121)
# Expected tables: notes, links, history, notifications, settings, chat_messages, llm_usage, events, tasks, items, feedback
```

### 3.2 Migration Steps

```bash
# 1. Backup SQLite
sg docker -c "docker exec para-organizer-app cp /var/lib/para-organizer/data/para.db /var/lib/para-organizer/data/para.db.backup"

# 2. Run Alembic migrations on PostgreSQL first
cd /home/agent/workspace/para-organizer
PARA_DB_URL=postgresql+asyncpg://para:password@localhost:5432/para alembic upgrade head

# 3. Run migration script
python3 scripts/migrate_pg.py
```

### 3.3 Data Integrity Checks

After migration, verify row counts match:

```bash
# SQLite counts
sg docker -c "docker exec para-organizer-app python3 -c \"
import sqlite3
conn = sqlite3.connect('/var/lib/para-organizer/data/para.db')
for t in ['notes','links','history','notifications','settings','chat_messages','llm_usage','events','tasks','items','feedback']:
    c = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'{t}: {c}')
conn.close()
\""

# PostgreSQL counts
psql -h localhost -U para -d para -c "
SELECT 'notes', COUNT(*) FROM notes UNION ALL
SELECT 'links', COUNT(*) FROM links UNION ALL
SELECT 'history', COUNT(*) FROM history UNION ALL
SELECT 'notifications', COUNT(*) FROM notifications UNION ALL
SELECT 'settings', COUNT(*) FROM settings UNION ALL
SELECT 'chat_messages', COUNT(*) FROM chat_messages UNION ALL
SELECT 'llm_usage', COUNT(*) FROM llm_usage UNION ALL
SELECT 'events', COUNT(*) FROM events UNION ALL
SELECT 'tasks', COUNT(*) FROM tasks UNION ALL
SELECT 'items', COUNT(*) FROM items UNION ALL
SELECT 'feedback', COUNT(*) FROM feedback;
"
```

### 3.4 Rollback Plan

```bash
# If migration fails:
# 1. Stop new containers
sg docker -c "docker compose down"

# 2. Drop and recreate PostgreSQL
sg docker -c "docker compose down para-db -v"

# 3. Restore old container (SQLite)
sg docker -c "docker start para-organizer-app"
```

---

## Phase 4: Deployment

### 4.1 Pre-deployment Checklist

```bash
# 1. Backup SQLite data
sg docker -c "docker exec para-organizer-app cp /var/lib/para-organizer/data/para.db /var/lib/para-organizer/data/para.db.backup.$(date +%Y%m%d_%H%M%S)"

# 2. Check current data volume
sg docker -c "docker exec para-organizer-app ls -la /var/lib/para-organizer/data/"

# 3. Ensure PostgreSQL and Redis are available
# (Use existing marcusx13db services or provision new ones)
```

### 4.2 Provision PostgreSQL & Redis

**Use existing services on this host:**

```bash
# PostgreSQL already running at localhost:5432 (marcusx13db)
# Redis already running at localhost:26379 (marcusx13db)

# Create PARA database
sg docker -c "docker exec marcusx13db-postgresql-egfqtp.1.4h3jhhhly703rppt2zxcg5fji \
  psql -U postgres -c \"CREATE DATABASE para;\"" 2>&1

# Create PARA user
sg docker -c "docker exec marcusx13db-postgresql-egfqtp.1.4h3jhhhly703rppt2zxcg5fji \
  psql -U postgres -d para -c \"CREATE USER para WITH PASSWORD 'password';\"" 2>&1

# Grant permissions
sg docker -c "docker exec marcusx13db-postgresql-egfqtp.1.4h3jhhhly703rppt2zxcg5fji \
  psql -U postgres -d para -c \"GRANT ALL PRIVILEGES ON DATABASE para TO para;\"" 2>&1

# Enable pgvector
sg docker -c "docker exec marcusx13db-postgresql-egfqtp.1.4h3jhhhly703rppt2zxcg5fji \
  psql -U postgres -d para -c \"CREATE EXTENSION IF NOT EXISTS vector;\"" 2>&1
```

### 4.3 Build & Deploy

```bash
cd /home/agent/workspace/para-organizer

# 1. Build new image
sg docker -c "docker compose build para-app" 2>&1

# 2. Stop current container
sg docker -c "docker stop para-organizer-app" 2>&1
sg docker -c "docker rm para-organizer-app" 2>&1

# 3. Start new stack
sg docker -c "docker compose up -d para-db para-redis" 2>&1

# Wait for DB and Redis to be healthy
sleep 10

# 4. Run Alembic migrations
sg docker -c "docker compose run --rm para-app alembic upgrade head" 2>&1

# 5. Run data migration
sg docker -c "docker compose run --rm para-app python3 scripts/migrate_pg.py" 2>&1

# 6. Start all services
sg docker -c "docker compose up -d" 2>&1
```

### 4.4 Environment Setup

Create `.env` file with real values:

```bash
cat > /home/agent/workspace/para-organizer/.env << 'EOF'
PARA_PORT=8731
PARA_DB_URL=postgresql+asyncpg://para:password@para-db:5432/para
PARA_REDIS_URL=redis://para-redis:6379/0
PARA_SECRET_KEY=change-me-in-production
LLM_PRIMARY=deepseek-v4-flash
LLM_FALLBACK=gpt-oss:20b
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN_HERE
TELEGRAM_WEBHOOK_URL=https://para.mxlabs.cloud/webhook/telegram
TELEGRAM_ALLOWED_USERS=8722556718
WEB_PUBLIC_URL=https://para.mxlabs.cloud
EMBED_PROVIDER=ollama_local
EMBED_BASE_URL=http://host.docker.internal:11434
EMBED_MODEL=nomic-embed-text
RAG_HYBRID_ENABLED=true
RAG_HYBRID_RATIO=0.5
EOF
```

### 4.5 Health Check Verification

```bash
# Check all services are up
sg docker -c "docker compose ps" 2>&1

# Check app health
curl -s http://localhost:8731/api/health/live
# Expected: {"status":"alive"}

curl -s http://localhost:8731/api/health/ready
# Expected: {"status":"ready"}

curl -s http://localhost:8731/api/health
# Expected: {"status":"healthy","version":"5.0.0","db":"connected","redis":"connected",...}
```

### 4.6 Rollback Procedure

```bash
# If something goes wrong:
cd /home/agent/workspace/para-organizer

# 1. Stop new stack
sg docker -c "docker compose down" 2>&1

# 2. Restore old container
sg docker -c "docker run -d \
  --name para-organizer-app \
  -p 8731:8731 \
  -v para-organizer_para-data:/var/lib/para-organizer/data \
  para-organizer:latest" 2>&1

# 3. Restore SQLite data
sg docker -c "docker exec para-organizer-app cp /var/lib/para-organizer/data/para.db.backup.* /var/lib/para-organizer/data/para.db"
```

---

## Phase 5: Verification

### 5.1 Smoke Tests

```bash
# 1. API is alive
curl -s http://localhost:8731/api/health/live | grep -q '"status":"alive"'
echo "Liveness: $?"

# 2. DB + Redis connected
curl -s http://localhost:8731/api/health | grep -q '"db":"connected"'
echo "DB connected: $?"

curl -s http://localhost:8731/api/health | grep -q '"redis":"connected"'
echo "Redis connected: $?"

# 3. Stats endpoint works
curl -s http://localhost:8731/api/stats | python3 -m json.tool
```

### 5.2 API Endpoint Verification

```bash
# Test core CRUD endpoints
# Create a note
curl -s -X POST http://localhost:8731/api/notes \
  -H "Content-Type: application/json" \
  -d '{"title":"Test Note","content":"This is a test","para_category":"Projects"}' | python3 -m json.tool

# List notes
curl -s http://localhost:8731/api/notes | python3 -m json.tool

# Search
curl -s "http://localhost:8731/api/search?q=test" | python3 -m json.tool

# Settings
curl -s http://localhost:8731/api/settings | python3 -m json.tool
```

### 5.3 Data Integrity Verification

```bash
# Compare row counts between old SQLite and new PostgreSQL
echo "=== SQLite (old) ==="
sg docker -c "docker exec para-organizer-app python3 -c \"
import sqlite3
conn = sqlite3.connect('/var/lib/para-organizer/data/para.db.backup.*')
for t in ['notes','links','history','settings','chat_messages','llm_usage']:
    c = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'{t}: {c}')
conn.close()
\""

echo "=== PostgreSQL (new) ==="
sg docker -c "docker exec marcusx13db-postgresql-egfqtp.1.4h3jhhhly703rppt2zxcg5fji \
  psql -U para -d para -c \"
    SELECT 'notes', COUNT(*) FROM notes UNION ALL
    SELECT 'links', COUNT(*) FROM links UNION ALL
    SELECT 'history', COUNT(*) FROM history UNION ALL
    SELECT 'settings', COUNT(*) FROM settings UNION ALL
    SELECT 'chat_messages', COUNT(*) FROM chat_messages UNION ALL
    SELECT 'llm_usage', COUNT(*) FROM llm_usage;\"" 2>&1
```

### 5.4 Performance Checks

```bash
# Response time test
time curl -s http://localhost:8731/api/stats > /dev/null

# Concurrent requests
for i in $(seq 1 10); do
  curl -s http://localhost:8731/api/notes?page=$i > /dev/null &
done
wait
echo "10 concurrent requests completed"
```

### 5.5 Worker & Scheduler Verification

```bash
# Check worker logs
sg docker -c "docker logs para-worker-1 --tail 20" 2>&1

# Check scheduler logs
sg docker -c "docker logs para-scheduler-1 --tail 20" 2>&1

# Publish a test task
curl -s -X POST http://localhost:8731/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"topic":"classify","payload":{"note_id":1}}'
```

---

## Summary of Files to Create/Modify

| File | Action | Description |
|---|---|---|
| `docker-compose.yml` | Modify | Remove `container_name` from scaled services, remove `version` |
| `.env.example` | Create | Template for environment variables |
| `.env` | Create | Real environment values |
| `Dockerfile` | Modify | Fix HEALTHCHECK endpoint |
| `scripts/wait-for-it.sh` | Create | Startup ordering helper |
| `scripts/migrate_pg.py` | Verify | Check schema alignment with live SQLite DB |
| `app/worker.py` | Modify | Port 7 handlers from sqlite → asyncpg, add backup handler |
| `app/backup.py` | Create | Cloud backup handler (only new module) |
| `app/scheduler_service.py` | Modify | Fix `archive`→`classify`, `autonomy`→`escalate` topic names |

## Execution Order

```
Phase 0 (Bug Fixes) → Phase 1 (Infra) → Phase 4.1-4.2 (Provision) →
Phase 3 (Migrate) → Phase 4.3-4.5 (Deploy) → Phase 5 (Verify) → Phase 2 (Worker Logic)
```

> **Note:** Phase 2 (worker logic) is listed last because the system can run without it initially — the stubs just log and sleep. Deploy the infrastructure first, then fill in handlers incrementally.
