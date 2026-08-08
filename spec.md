# PARA Organizer — Technical Specification

> **Version:** 3.0 | **Date:** 2025-07-25 | **Owner:** Marcus

## 1. Overview

A self-hosted note management system that automatically classifies notes into the PARA method (Projects / Areas / Resources / Archives) using LLM. Integrates with Hermes (MCP), Telegram (Bot), and Web UI. Receives cron job outputs from Hermes as notes.

### 1.1 What is PARA?

| Category | Meaning | Example |
|----------|---------|---------|
| **Projects** | Active work with deadline or specific goal | ต่อทะเบียนรถ ก่อน 15 ส.ค. |
| **Areas** | Ongoing responsibility, no end date | ดูแลเซิร์ฟเวอร์ Contabo |
| **Resources** | Reference material, useful info | สูตรผัดกะเพรา |
| **Archives** | Completed or no longer relevant | Completed projects |

### 1.2 Key Principles

1. **Auto-classify** — User writes a note, LLM figures out the PARA category
2. **Auto-deadline** — "ภายใน 15 ส.ค." → LLM extracts `2025-08-15`
3. **Auto-remind** — Telegram notification 7/3/1 days before deadline
4. **Auto-archive** — Completed notes archived after 30 days
5. **Multi-source** — Notes come from Hermes, Telegram, Web UI, or Cron
6. **Thai-first** — Notes are primarily in Thai; LLM must handle Thai well

---

## 2. Architecture

> **Production note (v5):** the diagram below reflects the deployed architecture — six
> Docker Compose services (`para-app`, `para-mcp`, `para-worker`, `para-scheduler`,
> `para-backup`, `para-redis`), managed by Dokploy, with an external PostgreSQL 16 +
> pgvector database. The earlier v4 design (single process, embedded SQLite) is
> preserved for historical reference on branch `backup/sqlite-version`.

```
┌──────────────────────────────────────────────────────┐
│                   USER TOUCHPOINTS                    │
│                                                       │
│  ┌─────────┐    ┌──────────┐    ┌─────────────────┐  │
│  │ Hermes  │    │ Telegram │    │    Web UI       │  │
│  │ (MCP)   │    │   Bot    │    │   (browser)     │  │
│  └────┬────┘    └────┬─────┘    └────────┬────────┘  │
│       │              │                   │            │
└───────┼──────────────┼───────────────────┼────────────┘
        │ HTTP SSE     │ Webhook           │ HTTP
        │ /mcp/sse     │ (long poll)       │
┌───────┼──────────────┼───────────────────┼────────────┐
│       ▼              ▼                   ▼            │
│      DOKPLOY-MANAGED DOCKER COMPOSE (production)       │
│                                                        │
│  ┌───────────────┐    ┌───────────────────────────┐   │
│  │   para-mcp    │    │         para-app           │   │
│  │  HTTP SSE     │    │      FastAPI :8731         │   │
│  │  27 tools     │    │  REST API + Telegram       │   │
│  │  :8100        │    │  webhook + Web UI          │   │
│  │  mc-para.     │    │  para.mxlabs.cloud         │   │
│  │  mxlabs.cloud │    │                             │   │
│  └───────┬───────┘    └──────────────┬──────────────┘   │
│          │                           │                   │
│          └─────────────┬─────────────┘                   │
│                        ▼                                 │
│              ┌────────────────────┐                      │
│              │     para-redis     │  task queue + cache   │
│              │     (Redis 7)      │                       │
│              └──────────┬─────────┘                      │
│                         │                                 │
│         ┌───────────────┼────────────────┐                │
│         ▼               ▼                ▼                │
│  ┌─────────────┐ ┌──────────────┐ ┌──────────────┐        │
│  │ para-worker │ │para-scheduler│ │ para-backup  │        │
│  │ classify /  │ │ (APScheduler,│ │ (optional    │        │
│  │ embed /link │ │  singleton)  │ │  cloud       │        │
│  │ (Redis      │ │              │ │  backup)     │        │
│  │  consumer)  │ │              │ │              │        │
│  └──────┬──────┘ └──────┬───────┘ └──────────────┘        │
│         │               │                                  │
│         ▼               ▼                                  │
│  ┌───────────────────────────────────────────────────┐     │
│  │           PostgreSQL 16 + pgvector (external)       │     │
│  │   notes | links | history | notifications           │     │
│  │   settings | chat_messages                           │     │
│  │   169.58.65.88:5436/paradb  (PARA_DB_URL)             │     │
│  └───────────────────────────────────────────────────┘     │
│                         │                                   │
│                         ▼                                   │
│                ┌──────────────────┐                         │
│                │    Notifier      │                         │
│                │  → Telegram      │                         │
│                │  → WebSocket     │                         │
│                └──────────────────┘                         │
└──────────────────────────────────────────────────────┘
        │                              │
        ▼                              ▼
┌────────────────┐          ┌─────────────────────┐
│  Ollama Cloud  │          │  Hermes Cron Jobs   │
│                │          │                     │
│  deepseek-v4   │          │  output → POST      │
│  -flash (main) │          │  /api/notes/cron    │
│                │          │                     │
│  gpt-oss:20b   │          │  blogwatcher → note │
│  (fallback)    │          │  server mon → note  │
└────────────────┘          └─────────────────────┘
```

---

## 3. Tech Stack

| Component | Technology | Version |
|-----------|-----------|---------|
| Backend | FastAPI | latest |
| Database (production, v5) | PostgreSQL + pgvector, external host `169.58.65.88:5436/paradb` (`PARA_DB_URL`) | PostgreSQL 16 |
| Database (legacy, v4) | SQLite (aiosqlite) — superseded; see `backup/sqlite-version` | built-in |
| DB access | SQLAlchemy 2.0 async (`asyncpg`) + Alembic migrations | latest |
| Task queue / cache | Redis (`para-redis` service) | 7 |
| LLM | Ollama Cloud (OpenAI-compatible API) | — |
| LLM Primary | `deepseek-v4-flash` | — |
| LLM Fallback | `gpt-oss:20b` | — |
| Embeddings | `nomic-embed-text` (via Ollama) | — |
| Scheduler | APScheduler, runs as dedicated `para-scheduler` singleton service | latest |
| Web UI | Jinja2 + HTMX + Tailwind CSS (CDN) | — |
| Full-text search (production) | PostgreSQL `tsvector` (generated column) | built-in |
| Full-text search (legacy, v4) | SQLite FTS5 — superseded; see `backup/sqlite-version` | built-in |
| Semantic search (production) | `pgvector` extension (embedding similarity) | — |
| Telegram | python-telegram-bot | latest |
| MCP (production) | `mcp` Python SDK, HTTP SSE transport (`app/mcp/mcp_server_http.py`), 27 tools | latest |
| MCP (legacy/local-dev) | stdio transport (`app/mcp/mcp_server.py`) | latest |
| Export | markdown + JSON | — |
| Deploy | Docker Compose (6 services) on Dokploy, GitHub Flow (merge to `main` = deploy) | — |
| Storage (legacy, v4) | `/var/lib/para-organizer/` (local SQLite file + systemd) — superseded | — |
| Python | 3.12+ | — |

---

## 4. Environment Variables (.env)

> **v5 note:** production storage moved from a local SQLite file to PostgreSQL. The
> `PARA_DB_PATH` variable below is retained only for the legacy SQLite build on
> `backup/sqlite-version`; production sets `PARA_DB_URL` (and the Redis/pool
> variables) instead, as configured in `docker-compose.yml` / `app/config.py`.

```bash
# ─── Core ───
PARA_PORT=8731
PARA_DB_PATH=/var/lib/para-organizer/data/para.db   # legacy (v4/SQLite) only
PARA_DB_URL=postgresql+asyncpg://paradb:<pass>@169.58.65.88:5436/paradb  # production (v5)
PARA_DB_POOL_SIZE=10
PARA_DB_MAX_OVERFLOW=20
PARA_REDIS_URL=redis://para-redis:6379/0
PARA_REDIS_CACHE_TTL=60
PARA_SECRET_KEY=change-me-in-production

# ─── LLM (Ollama Cloud) ───
OLLAMA_API_KEY=<key>
OLLAMA_BASE_URL=https://ollama.com/v1
LLM_PRIMARY=deepseek-v4-flash
LLM_FALLBACK=gpt-oss:20b
LLM_TIMEOUT=60
LLM_MAX_RETRIES=2

# ─── Embeddings (semantic search / pgvector) ───
EMBED_PROVIDER=ollama_local
EMBED_BASE_URL=http://localhost:11434
EMBED_MODEL=nomic-embed-text

# ─── Chat (conversational mode) ───
CHAT_MODEL=gpt-oss:20b
CHAT_HISTORY_MAX=20

# ─── Telegram ───
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_WEBHOOK_URL=https://para.mxlabs.cloud/webhook/telegram
TELEGRAM_ALLOWED_USERS=<comma-separated-user-ids>

# ─── Notification ───
NOTIFY_CHANNEL=telegram
NOTIFY_DEADLINE_DAYS=7,3,1
NOTIFY_DIGEST_DAY=mon
NOTIFY_DIGEST_TIME=08:00
NOTIFY_STALE_DAYS=14

# ─── Auto ───
AUTO_ARCHIVE_DAYS=30
RECLASSIFY_INTERVAL_HOURS=6
RECLASSIFY_CONFIDENCE_THRESHOLD=0.7

# ─── Web ───
WEB_PUBLIC_URL=https://para.mxlabs.cloud
```

---

## 5. Database Schema

> **Production (v5) storage:** the schema below was designed for the original SQLite
> build (v4) and is preserved as-is for historical reference — the full legacy
> implementation lives on branch `backup/sqlite-version` (`app/database.py`,
> `app/models.py`). Production now runs on **PostgreSQL 16 + pgvector**
> (`PARA_DB_URL`, external host `169.58.65.88:5436/paradb`), accessed via async
> SQLAlchemy 2.0 (`app/database_v2.py`'s `async_session_factory`, ORM models in
> `app/models_v2.py`) with Alembic migrations. The same logical tables exist
> (`notes`, `links`, `history`, `notifications`, `settings`, `chat_messages`), with
> these production-specific differences from the SQLite DDL shown below:
> - `tags` and `source_metadata` are `JSONB` columns instead of TEXT-encoded JSON.
> - Full-text search is a generated `tsvector` column (`search_vector`, `GENERATED
>   ALWAYS AS ... STORED`) populated via `to_tsvector('simple', title || content)`,
>   not a separate FTS5 virtual table/triggers (see §5.7 below for the legacy
>   FTS5 approach and its production replacement).
> - `notes` has an additional `embedding vector(768)` column (via the `pgvector`
>   extension) storing `nomic-embed-text` embeddings for semantic search.
> - IDs are still integer primary keys; timestamps use `timestamptz` via
>   SQLAlchemy `DateTime(timezone=True)` rather than SQLite `DATETIME`.

### 5.1 `notes` table (legacy v4 / SQLite — see production note above)

```sql
CREATE TABLE notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    para_category TEXT NOT NULL DEFAULT 'inbox',
        -- inbox | projects | areas | resources | archives
    sub_category TEXT,
    status TEXT NOT NULL DEFAULT 'active',
        -- active | completed | archived
    priority TEXT NOT NULL DEFAULT 'medium',
        -- low | medium | high | urgent
    deadline DATE,
        -- ISO 8601 date or NULL
    tags TEXT NOT NULL DEFAULT '[]',
        -- JSON array of strings
    source TEXT NOT NULL DEFAULT 'manual',
        -- manual | hermes | telegram | cron
    source_metadata TEXT NOT NULL DEFAULT '{}',
        -- JSON: {chat_id, message_id, cron_job_name, ...}
    llm_model TEXT,
    llm_confidence REAL NOT NULL DEFAULT 0.0,
        -- 0.0 to 1.0
    llm_reasoning TEXT,
        -- Thai explanation of classification
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_at DATETIME
);

CREATE INDEX idx_notes_category ON notes(para_category);
CREATE INDEX idx_notes_status ON notes(status);
CREATE INDEX idx_notes_deadline ON notes(deadline);
CREATE INDEX idx_notes_source ON notes(source);
```

### 5.2 `links` table

```sql
CREATE TABLE links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_note_id INTEGER NOT NULL,
    to_note_id INTEGER NOT NULL,
    link_type TEXT NOT NULL DEFAULT 'related',
        -- related | depends_on | refines
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (from_note_id) REFERENCES notes(id) ON DELETE CASCADE,
    FOREIGN KEY (to_note_id) REFERENCES notes(id) ON DELETE CASCADE
);

CREATE INDEX idx_links_from ON links(from_note_id);
CREATE INDEX idx_links_to ON links(to_note_id);
```

### 5.3 `history` table

```sql
CREATE TABLE history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    action TEXT NOT NULL,
        -- created | classified | moved | archived | edited | deadline_reminded
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);

CREATE INDEX idx_history_note ON history(note_id);
```

### 5.4 `notifications` table

```sql
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER,
    type TEXT NOT NULL,
        -- deadline | stale | digest | reclassify
    channel TEXT NOT NULL DEFAULT 'telegram',
    status TEXT NOT NULL DEFAULT 'pending',
        -- pending | sent | failed
    scheduled_at DATETIME NOT NULL,
    sent_at DATETIME,
    payload TEXT,
        -- JSON: message content
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);

CREATE INDEX idx_notif_status ON notifications(status);
CREATE INDEX idx_notif_scheduled ON notifications(scheduled_at);
```

### 5.5 `settings` table

```sql
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

### 5.6 `chat_messages` table

Conversation history for chat mode, persisted per Telegram `chat_id` so it survives
restarts. Trimmed to the last `CHAT_HISTORY_MAX` messages per chat on every append.

```sql
CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    role TEXT NOT NULL,           -- 'user' | 'assistant'
    content TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_chat_messages_chat ON chat_messages(chat_id, id);
```

### 5.7 FTS5 (Full-text search) — legacy v4/SQLite

> **Production (v5) replacement:** full-text search no longer uses a separate FTS5
> virtual table + sync triggers. It's a generated `tsvector` column on `notes`
> (`search_vector`, populated by `to_tsvector('simple', title || content)`),
> queried directly with PostgreSQL's `@@`/`ts_rank` operators — no triggers needed
> since it's `GENERATED ALWAYS AS ... STORED`. Semantic search is handled
> separately via the `embedding vector(768)` column and the `pgvector` extension
> (cosine distance, `<=>` operator). The FTS5 DDL below is preserved for
> historical reference (`backup/sqlite-version`).

```sql
CREATE VIRTUAL TABLE notes_fts USING fts5(
    title, content, tags,
    content='notes',
    content_rowid='id'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;

CREATE TRIGGER notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
END;

CREATE TRIGGER notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO notes_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;
```

---

## 6. LLM Classifier

### 6.1 Classification Prompt

```
You are a PARA note classifier. Classify this note into exactly one of:
- "projects": Active work with a deadline or specific goal
- "areas": Ongoing responsibility, no end date
- "resources": Reference material, useful info, no action needed
- "archives": Completed or no longer relevant

Also extract:
- sub_category: short label (1-3 words)
- priority: low | medium | high
- deadline: ISO date (YYYY-MM-DD) if found in text, else null
- tags: array of 3-7 relevant tags (mix Thai and English as appropriate)
- confidence: 0.0 to 1.0
- reasoning: short explanation in Thai (1-2 sentences)

Respond as JSON ONLY. No markdown, no explanation outside JSON.

Note title: {title}
Note content: {content}
```

### 6.2 Expected JSON Output

```json
{
  "para_category": "projects",
  "sub_category": "Vehicle Registration",
  "priority": "high",
  "deadline": "2025-08-15",
  "tags": ["รถยนต์", "เอกสาร", "deadline"],
  "confidence": 0.95,
  "reasoning": "มีกำหนดเวลาชัดเจน (15 ส.ค. 2025) และมีเป้าหมายเฉพาะคือการต่อทะเบียนรถ"
}
```

### 6.3 LLM Call Logic

```python
async def classify_note(title: str, content: str) -> dict:
    """
    Call LLM with fallback.
    Returns classification dict.
    """
    prompt = CLASSIFY_PROMPT.format(title=title, content=content)

    for model in [LLM_PRIMARY, LLM_FALLBACK]:
        try:
            response = await call_ollama(model, prompt, format="json")
            result = json.loads(response)
            # Validate
            assert result["para_category"] in PARA_CATEGORIES
            assert 0.0 <= result["confidence"] <= 1.0
            result["llm_model"] = model
            return result
        except (json.JSONDecodeError, KeyError, AssertionError, TimeoutError) as e:
            logger.warning(f"LLM {model} failed: {e}, trying fallback...")
            continue

    # All models failed → default to inbox
    return {
        "para_category": "inbox",
        "sub_category": None,
        "priority": "medium",
        "deadline": None,
        "tags": [],
        "confidence": 0.0,
        "llm_model": None,
        "reasoning": "LLM classification failed, placed in inbox"
    }
```

### 6.4 Ollama Cloud API Call

```python
async def call_ollama(model: str, prompt: str, format: str = None) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if format:
        payload["format"] = format

    async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
        resp = await client.post(
            f"{OLLAMA_BASE_URL}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {OLLAMA_API_KEY}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
```

### 6.5 Tested Results (2025-07-25)

Tested with 3 Thai notes against 4 models. All passed:

| Model | Thai Understanding | Deadline Extraction | Tags in Thai | Confidence |
|-------|-------------------|--------------------:|:------------|:----------|
| deepseek-v4-flash | ✅ Excellent | ✅ 2025-08-15 | ✅ Yes | 0.95 |
| gpt-oss:20b | ✅ Good | ✅ 2025-08-15 | ⚠️ Mixed | 0.90 |
| gemma4:31b | ✅ Excellent | ✅ 2025-08-15 | ✅ Yes | 1.00 |
| nemotron-3-nano:30b | ✅ Good | ✅ 2025-08-15 | ✅ Yes | 0.92 |

**Selected:** `deepseek-v4-flash` (primary) + `gpt-oss:20b` (fallback)

---

## 7. REST API

### 7.1 Notes

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/api/notes` | Create note (auto-classify) | API key |
| `POST` | `/api/notes/cron` | Create note from Hermes cron | API key |
| `GET` | `/api/notes` | List notes (filter by category, status, source) | — |
| `GET` | `/api/notes/{id}` | Get single note | — |
| `PUT` | `/api/notes/{id}` | Update note | — |
| `DELETE` | `/api/notes/{id}` | Delete note | — |
| `POST` | `/api/notes/{id}/move` | Move to different PARA category | — |
| `POST` | `/api/notes/{id}/archive` | Archive note | — |
| `POST` | `/api/classify/{id}` | Re-classify note with LLM | — |

### 7.2 PARA

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/para/tree` | Get PARA tree structure with counts |

### 7.3 Search

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/search?q={query}` | Full-text search — PostgreSQL `tsvector` in production (v5); FTS5 in the legacy SQLite build |
| `GET` | `/api/search/suggest?q={query}` | Search suggestions |

### 7.4 Stats

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/stats` | Summary statistics |
| `GET` | `/api/deadlines?days={n}` | Upcoming deadlines within N days |
| `GET` | `/api/digest` | Weekly digest data |

### 7.5 Export

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/export?format=md` | Export as markdown zip |
| `GET` | `/api/export?format=json` | Export as JSON |

### 7.6 Request/Response Examples

#### Create Note

```http
POST /api/notes
Content-Type: application/json
Authorization: Bearer <api-key>

{
  "title": "ต่อทะเบียนรถ",
  "content": "ทะเบียนหมดอายุ 15 สิงหาคม 2025 ไปที่ ขน. ต้องเตรียม สำเนาทะเบียนบ้าน บัตรประชาชน",
  "source": "hermes"
}
```

Response (200):
```json
{
  "id": 42,
  "title": "ต่อทะเบียนรถ",
  "content": "ทะเบียนหมดอายุ 15 สิงหาคม 2025...",
  "para_category": "projects",
  "sub_category": "Vehicle Registration",
  "status": "active",
  "priority": "high",
  "deadline": "2025-08-15",
  "tags": ["รถยนต์", "เอกสาร", "deadline"],
  "source": "hermes",
  "llm_model": "deepseek-v4-flash",
  "llm_confidence": 0.95,
  "llm_reasoning": "มีกำหนดเวลาชัดเจน และมีเป้าหมายเฉพาะ",
  "created_at": "2025-07-25T21:00:00Z",
  "updated_at": "2025-07-25T21:00:00Z"
}
```

#### Create Note from Cron

```http
POST /api/notes/cron
Content-Type: application/json
Authorization: Bearer <api-key>

{
  "content": "Server health: all online, disk 45%, no alerts",
  "source": "cron:server-health-check",
  "auto_classify": true,
  "tags_override": ["server", "health-check"]
}
```

#### List Notes

```http
GET /api/notes?category=projects&status=active&limit=20&offset=0
```

Response:
```json
{
  "notes": [...],
  "total": 5,
  "limit": 20,
  "offset": 0
}
```

#### Search

```http
GET /api/search?q=ทะเบียน&limit=10
```

Response:
```json
{
  "results": [
    {
      "id": 42,
      "title": "ต่อทะเบียนรถ",
      "snippet": "...ทะเบียนหมดอายุ 15 สิงหาคม...",
      "para_category": "projects",
      "rank": -3.2
    }
  ],
  "total": 1
}
```

#### Deadlines

```http
GET /api/deadlines?days=14
```

Response:
```json
{
  "deadlines": [
    {
      "id": 42,
      "title": "ต่อทะเบียนรถ",
      "deadline": "2025-08-15",
      "days_left": 7,
      "priority": "high"
    }
  ]
}
```

---

## 8. MCP Server (Hermes Integration)

> **Production (v5) transport:** Hermes talks to PARA over **HTTP SSE**, not a
> local stdio subprocess. The production server is `app/mcp/mcp_server_http.py`,
> running as its own service (`para-mcp`, port 8100, host `mc-para.mxlabs.cloud`,
> SSE endpoint `https://mc-para.mxlabs.cloud/mcp/sse`), backed by PostgreSQL via
> async SQLAlchemy (`app/database_v2.py`'s `async_session_factory`,
> `app/models_v2.py`). The original stdio server, `app/mcp/mcp_server.py` (§8.1
> config below), still exists but is legacy/local-dev only — it is not the
> production transport. Both servers expose the same **27-tool** set (verified via
> `grep -c "@mcp.tool()" app/mcp/mcp_server.py`); the table in §8.2 shows the
> original 10-tool set from the v1 design and is no longer exhaustive.

### 8.1 Configuration

#### Production (HTTP SSE)

```yaml
# ~/.hermes/config.yaml
mcp:
  servers:
    para-organizer:
      url: https://mc-para.mxlabs.cloud/mcp/sse
      transport: sse
```

#### Legacy / local-dev (stdio)

```yaml
# ~/.hermes/config.yaml
mcp:
  servers:
    para-organizer:
      command: python3
      args: ["/var/lib/para-organizer/app/mcp/mcp_server.py"]
      env:
        PARA_DB: /var/lib/para-organizer/data/para.db   # legacy SQLite path
        OLLAMA_API_KEY: ${OLLAMA_API_KEY}
        OLLAMA_BASE_URL: https://ollama.com/v1
```

### 8.2 MCP Tools (original v1 set — 10 of the current 27; not exhaustive)

| Tool Name | Parameters | Returns | Description |
|-----------|-----------|---------|-------------|
| `para_add_note` | `title: str`, `content: str` | `Note` | Create + auto-classify |
| `para_search` | `query: str`, `category?: str`, `limit?: int` | `Note[]` | Full-text search |
| `para_list` | `category?: str`, `status?: str`, `limit?: int` | `Note[]` | List by category |
| `para_get` | `id: int` | `Note` | Get single note |
| `para_move` | `id: int`, `category: str` | `Note` | Move PARA category |
| `para_archive` | `id: int` | `Note` | Archive note |
| `para_stats` | — | `Stats` | Summary stats |
| `para_deadlines` | `days_ahead?: int` | `Deadline[]` | Upcoming deadlines |
| `para_digest` | — | `Digest` | Weekly digest |
| `para_add_link` | `from_id: int`, `to_id: int`, `link_type?: str` | `Link` | Link notes |

The remaining 17 tools (task/chat/graph/recurrence-related additions since v1) are
defined in `app/mcp/mcp_server.py` / `app/mcp/mcp_server_http.py`; consult those
files for the current full list and signatures.

### 8.3 Example MCP Tool Definition

```python
@mcp.tool()
async def para_add_note(title: str, content: str) -> dict:
    """Add a note to PARA Organizer. Auto-classifies with LLM.

    Args:
        title: Note title (short summary)
        content: Note content (details)

    Returns:
        Created note with LLM classification
    """
    note = await create_note(title=title, content=content, source="hermes")
    return note.to_dict()
```

---

## 9. Telegram Bot

### 9.1 Webhook Setup

```python
POST /webhook/telegram
# Receives Telegram updates
# Parses commands
# Creates notes / returns results
```

### 9.2 Commands

| Command | Example | Action |
|---------|---------|--------|
| `/ask <question>` | `/ask ควรวางแผนงานนี้ยังไงดี` | Start/continue a chat conversation |
| `/note <text>` | `/note ต้องต่อทะเบียนรถ ก่อน 15 ส.ค.` | Create note directly from text |
| `/note` (no text) | `/note` | Distill the current conversation into a note, then reset history |
| `/reset`, `/clear` | `/reset` | Wipe the conversation history for this chat |
| `/list` | `/list` | Show all (inline buttons) |
| `/list <cat>` | `/list projects` | Show by category |
| `/search <q>` | `/search ทะเบียน` | Search notes |
| `/deadlines` | `/deadlines` | Show upcoming deadlines |
| `/done <id>` | `/done 42` | Archive note |
| `/move <id> <cat>` | `/move 42 resources` | Move category |
| `/stats` | `/stats` | Show statistics |
| `/digest` | `/digest` | Generate digest now |
| `/help` | `/help` | Show all commands |

### 9.3 Chat Mode (plain text, no command)

Plain text (no leading `/`) is a **conversation** with the bot, not an instant note. The
bot (`app/chat.py`, `CHAT_MODEL` setting) answers using retrieved context from the user's
PARA notes — a full-text search on the message's keywords (PostgreSQL `tsvector` in
production; FTS5 `notes_fts` in the legacy SQLite build), plus upcoming
deadlines and quick stats — and the last `CHAT_HISTORY_MAX` messages of conversation
history, persisted per `chat_id` in the `chat_messages` table so it survives restarts.

```
User: มีงานอะไรค้างอยู่บ้างที่ deadline ใกล้ที่สุด
Bot:  ตอนนี้มีงาน "ต่อทะเบียนรถ" deadline 15 ส.ค. ใกล้ที่สุดครับ...
```

To turn a conversation into a note, send `/note` with no text — the chat model distills
the conversation into a title + content, runs it through the same classify/store pipeline
as `/note <text>`, and then clears the conversation history so the next chat starts fresh.
All destructive/mutating actions (archive, move, etc.) remain command-only; chat mode never
creates or changes notes on its own.

### 9.4 Notification Messages

#### Deadline Reminder

```
⏰ ใกล้ถึงกำหนด!

📋 ต่อทะเบียนรถ
📅 Deadline: 15 ส.ค. 2025
⏰ เหลือ: 7 วัน
🔴 Priority: High

🔗 ดูรายละเอียด: https://para.mxlabs.cloud/notes/42
```

#### Weekly Digest

```
🧠 PARA Weekly Digest
21-27 ก.ค. 2025

📊 สรุป:
  Notes ทั้งหมด: 52
  • Projects: 5 (ลด 1)
  • Areas: 8
  • Resources: 15
  • Archives: 24 (เพิ่ม 1)

✅ เสร็จสิ้นสัปดาห์นี้:
  • Dashboard Renderer → Archived

🔴 กำลังทำ:
  • ai-vdo-gen (deadline พรุ่งนี้)
  • PARA Organizer (เพิ่งเริ่ม)

⚠️ Stale (ไม่อัปเดต > 14 วัน):
  • MCP Server - Server Manager

📝 Notes ใหม่ 5 อันสัปดาห์นี้
```

---

## 10. Scheduler (APScheduler)

> **Production (v5):** the scheduler runs as its own singleton service,
> `para-scheduler`, in the production `docker-compose.yml` (`python3 -m
> app.scheduler_service`), rather than as a background task embedded in the
> `para-app` process. This avoids duplicate job execution when `para-app` is
> scaled. The job table and cron logic below are unchanged.

| Schedule | Job | Action |
|----------|-----|--------|
| Every 6 hours | `reclassify_job` | Re-classify notes where `confidence < 0.7` |
| Daily 02:00 | `auto_archive_job` | Archive notes where `status=completed` for > 30 days |
| Daily 09:00 | `deadline_check_job` | Check deadlines (7/3/1 days) → send Telegram |
| Daily 18:00 | `stale_project_job` | Check projects not updated > 14 days → send Telegram |
| Monday 08:00 | `weekly_digest_job` | Generate digest → send Telegram |

### 10.1 Job Implementation

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

scheduler = AsyncIOScheduler()

# Reclassify low-confidence notes every 6 hours
scheduler.add_job(
    reclassify_low_confidence_notes,
    CronTrigger(hour="*/6"),
    id="reclassify",
    name="Reclassify low-confidence notes",
)

# Auto-archive daily at 2 AM
scheduler.add_job(
    auto_archive_completed,
    CronTrigger(hour=2, minute=0),
    id="auto_archive",
    name="Auto-archive completed notes",
)

# Deadline check daily at 9 AM
scheduler.add_job(
    check_deadlines_and_notify,
    CronTrigger(hour=9, minute=0),
    id="deadline_check",
    name="Check deadlines and send notifications",
)

# Stale project check daily at 6 PM
scheduler.add_job(
    check_stale_projects,
    CronTrigger(hour=18, minute=0),
    id="stale_check",
    name="Check stale projects",
)

# Weekly digest every Monday at 8 AM
scheduler.add_job(
    send_weekly_digest,
    CronTrigger(day_of_week="mon", hour=8, minute=0),
    id="weekly_digest",
    name="Send weekly digest",
)
```

---

## 11. Web UI

### 11.1 Pages

| Route | Template | Description |
|-------|----------|-------------|
| `GET /` | `index.html` | PARA kanban view (4 columns) |
| `GET /notes/{id}` | `note_detail.html` | Single note view |
| `GET /new` | `note_new.html` | Create note form |
| `GET /stats` | `stats.html` | Statistics dashboard |
| `GET /search?q=` | `search.html` | Search results |

### 11.2 PARA Kanban Layout

```
┌────────────┬────────────┬────────────┬────────────┐
│  Projects  │   Areas    │ Resources  │  Archives  │
│    (5)     │    (8)     │   (15)     │    (24)    │
├────────────┼────────────┼────────────┼────────────┤
│ 🔴 Note 1  │ 📌 Note 6  │ 📚 Note 11 │ 📦 Note 20 │
│ 🔴 Note 2  │ 📌 Note 7  │ 📚 Note 12 │ 📦 Note 21 │
│ 🟡 Note 3  │ 📌 Note 8  │ 📚 Note 13 │ 📦 Note 22 │
│ 🟡 Note 4  │ 📌 Note 9  │ 📚 Note 14 │            │
│ 🟢 Note 5  │ 📌 Note 10 │ 📚 Note 15 │            │
│            │            │ 📚 Note 16 │            │
│            │            │ 📚 Note 17 │            │
└────────────┴────────────┴────────────┴────────────┘
```

### 11.3 Tech

- **Jinja2** templates
- **HTMX** for dynamic interactions (no SPA framework)
- **Tailwind CSS** via CDN
- Sortable notes (drag-and-drop between columns via HTMX)
- Note cards show: title, priority dot, deadline badge, tags

---

## 12. Project Structure

> **Production (v5) note:** `database.py` / `models.py` (aiosqlite connection +
> Pydantic/SQLite models) are the legacy v4 layer, preserved on
> `backup/sqlite-version`. Production adds `database_v2.py` (async SQLAlchemy
> engine/session factory for PostgreSQL) and `models_v2.py` (SQLAlchemy ORM models,
> incl. `pgvector` embedding column and generated `tsvector` search column), plus
> `worker.py` (Redis-consuming background worker), `scheduler_service.py`
> (standalone scheduler entrypoint), `mcp/mcp_server_http.py` (production HTTP SSE
> MCP server), and `vector_store.py` (pgvector-backed embedding store). The tree
> below is the original v1 layout; treat `database.py`/`models.py`/`mcp_server.py`
> as legacy where it conflicts with the above.

```
para-organizer/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app + startup + scheduler init
│   ├── config.py               # Settings (pydantic-settings)
│   ├── database.py             # legacy (v4): SQLite connection + migrations
│   ├── database_v2.py          # production (v5): async SQLAlchemy engine/session for PostgreSQL
│   ├── models.py               # legacy (v4): Pydantic/SQLite models
│   ├── models_v2.py            # production (v5): SQLAlchemy ORM models (pgvector, tsvector)
│   ├── worker.py               # production (v5): Redis-consuming background worker (embed/link/classify)
│   ├── scheduler_service.py    # production (v5): standalone scheduler entrypoint (para-scheduler)
│   ├── vector_store.py         # production (v5): pgvector-backed embedding store
│   │
│   ├── classifier.py           # LLM classification
│   │   ├── CLASSIFY_PROMPT
│   │   ├── async def classify_note(title, content) -> dict
│   │   ├── async def call_ollama(model, prompt, format) -> str
│   │   └── def extract_deadline_from_text(text) -> date | None
│   │
│   ├── scheduler.py            # APScheduler jobs
│   │   ├── async def reclassify_low_confidence_notes()
│   │   ├── async def auto_archive_completed()
│   │   ├── async def check_deadlines_and_notify()
│   │   ├── async def check_stale_projects()
│   │   └── async def send_weekly_digest()
│   │
│   ├── notifier.py             # Notification router
│   │   ├── async def send_telegram(chat_id, text)
│   │   └── async def notify_deadline(note, days_left)
│   │
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── notes.py            # /api/notes/*
│   │   ├── para.py             # /api/para/*
│   │   ├── search.py           # /api/search/*
│   │   ├── stats.py            # /api/stats, /api/deadlines, /api/digest
│   │   ├── export.py           # /api/export
│   │   ├── telegram_webhook.py # /webhook/telegram
│   │   ├── cron_webhook.py     # /api/notes/cron
│   │   └── pages.py            # Web UI pages (Jinja2)
│   │
│   ├── integrations/
│   │   ├── __init__.py
│   │   └── telegram_bot.py     # Telegram command handlers
│   │
│   ├── mcp/
│   │   ├── __init__.py
│   │   ├── mcp_server.py       # legacy/local-dev (v4): stdio MCP server for Hermes
│   │   └── mcp_server_http.py  # production (v5): HTTP SSE MCP server (para-mcp service)
│   │
│   ├── templates/
│   │   ├── base.html           # Layout: nav + Tailwind CDN
│   │   ├── index.html          # PARA kanban
│   │   ├── note_detail.html    # Single note
│   │   ├── note_new.html       # Create form
│   │   ├── stats.html          # Stats dashboard
│   │   └── search.html         # Search results
│   │
│   └── static/
│       └── app.js              # HTMX helpers
│
├── data/
│   └── para.db                 # legacy (v4): local SQLite file (gitignored); production (v5) uses external PostgreSQL, no local data dir
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py             # Fixtures: test client, test DB
│   ├── test_classifier.py      # LLM classification tests
│   ├── test_api.py             # REST API tests
│   ├── test_telegram.py        # Telegram webhook tests
│   ├── test_scheduler.py       # Scheduler job tests
│   └── test_mcp.py             # MCP server tests
│
├── scripts/
│   ├── init_db.py              # Initialize database
│   └── seed.py                 # Seed test data
│
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
└── para-organizer.service      # systemd unit file
```

---

## 13. Dependencies (requirements.txt)

> **v5 note:** production added a PostgreSQL/async-SQLAlchemy stack and Redis
> alongside the original dependencies. `aiosqlite` and `sqlite-vec` are still
> present in `requirements.txt` for the legacy SQLite code path
> (`backup/sqlite-version`), but are not what production runs against.

```
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
pydantic>=2.0
pydantic-settings>=2.0
httpx>=0.27.0
apscheduler>=3.10.0
python-telegram-bot>=21.0
jinja2>=3.1.0
python-multipart>=0.0.9
mcp>=1.0.0,<2.0.0
pytest>=8.0.0
pytest-asyncio>=0.23.0

# ─── Legacy (v4 / SQLite) ───
aiosqlite>=0.20.0
sqlite-vec>=0.1.0

# ─── Production (v5 / PostgreSQL) ───
asyncpg>=0.29.0
sqlalchemy[asyncio]>=2.0
alembic>=1.13.0
psycopg2-binary>=2.9.0
redis[hiredis]>=5.0.0
pgvector>=0.2.0
```

---

## 14. Docker

### 14.1 Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /var/lib/para-organizer/data

EXPOSE 8731

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8731"]
```

### 14.2 docker-compose.yml

> **v5 note:** the single-service compose file below was the original v1 design
> (one container, local SQLite volume). Production runs a **six-service** compose
> stack, built from the same `Dockerfile` but with different `command:`s per
> service, deployed via **Dokploy** (self-hosted PaaS) rather than plain
> `docker compose up`. All services run `replicas: 1` (single-user deployment).
> Key differences from the sketch below:
> - No local SQLite volume — `PARA_DB_URL` points at the external PostgreSQL host
>   (`169.58.65.88:5436/paradb`); there's no `./data` bind mount for the DB.
> - Six services: `para-app` (FastAPI REST + Web UI + Telegram webhook, :8731,
>   `para.mxlabs.cloud`), `para-mcp` (MCP HTTP SSE, :8100, `mc-para.mxlabs.cloud`),
>   `para-worker` (Redis-queue task consumer — embeddings/linking/classification),
>   `para-scheduler` (APScheduler singleton), `para-backup` (optional cloud
>   backup), `para-redis` (Redis 7, task queue + cache).
> - `para-app` and `para-mcp` are exposed to the internet via Traefik labels
>   (`traefik.enable=true`, host-based routing, TLS via Let's Encrypt) on a
>   `dokploy-network`, plus an internal `para-overlay` network shared by all
>   services.
> - Deploy model is GitHub Flow: merging to `main` triggers Dokploy to pull and
>   redeploy from the repo — there's no manual `docker compose up` step in normal
>   operation.
>
> See the real `docker-compose.yml` in the repo root for the full definition
> (env vars, healthchecks, resource limits, Traefik labels). Abbreviated shape:

```yaml
services:
  para-redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes --maxmemory 512mb --maxmemory-policy allkeys-lru
    deploy: { replicas: 1 }

  para-app:                       # FastAPI REST + Web UI + Telegram webhook
    build: { context: ., dockerfile: Dockerfile }
    command: python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8731
    ports: ["8731:8731"]
    environment:
      - PARA_DB_URL=postgresql+asyncpg://paradb:***@169.58.65.88:5436/paradb
      - PARA_REDIS_URL=redis://para-redis:6379/0
    depends_on: { para-redis: { condition: service_healthy } }
    deploy: { replicas: 1 }
    labels:
      - "traefik.http.routers.para-app.rule=Host(`para.mxlabs.cloud`)"

  para-mcp:                       # MCP HTTP SSE server (production transport)
    build: { context: ., dockerfile: Dockerfile }
    command: python3 -m app.mcp.mcp_server_http
    environment:
      - PARA_DB_URL=postgresql+asyncpg://paradb:***@169.58.65.88:5436/paradb
    deploy: { replicas: 1 }
    labels:
      - "traefik.http.routers.para-mcp.rule=Host(`mc-para.mxlabs.cloud`)"

  para-worker:                    # background task consumer (embed/link/classify)
    build: { context: ., dockerfile: Dockerfile }
    command: python3 -m app.worker
    depends_on: { para-redis: { condition: service_healthy } }
    deploy: { replicas: 1 }

  para-scheduler:                 # APScheduler singleton
    build: { context: ., dockerfile: Dockerfile }
    command: python3 -m app.scheduler_service
    deploy: { replicas: 1 }

  para-backup:                    # optional cloud backup
    build: { context: ., dockerfile: Dockerfile }
    command: python3 -m app.scheduler_service --backup-only
    deploy: { replicas: 1 }

networks:
  para-overlay: { driver: overlay, attachable: true }
  dokploy-network: { external: true }
```

---

## 15. systemd (legacy v4 bare-metal deploy — superseded)

> **Production (v5) note:** production is no longer deployed via systemd on bare
> metal. It runs as the Docker Compose stack in §14.2, managed by **Dokploy**
> (self-hosted PaaS), with deploys triggered by merges to `main` (GitHub Flow).
> The unit file below reflects the original v1/v4 deployment model and is kept
> for historical reference only.

### para-organizer.service

```ini
[Unit]
Description=PARA Organizer
After=network.target

[Service]
Type=simple
User=agent
WorkingDirectory=/var/lib/para-organizer
EnvironmentFile=/var/lib/para-organizer/.env
ExecStart=/var/lib/para-organizer/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8731
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## 16. Testing Requirements

### 16.1 Unit Tests

- `test_classifier.py`: Mock LLM responses, test parsing, test fallback
- `test_api.py`: Test all REST endpoints with httpx AsyncClient
- `test_telegram.py`: Test webhook parsing, command handling
- `test_scheduler.py`: Test job logic (mock time)
- `test_mcp.py`: Test MCP tool responses

### 16.2 Integration Tests

- Create note via API → verify LLM classification
- Create note via Telegram webhook → verify in DB
- Search → verify FTS5 results
- Deadline check → verify notification queued

### 16.3 Test Data

```python
# tests/conftest.py
TEST_NOTES = [
    {
        "title": "ต่อทะเบียนรถ",
        "content": "ทะเบียนหมดอายุ 15 สิงหาคม 2025",
        "expected_category": "projects",
        "expected_deadline": "2025-08-15",
    },
    {
        "title": "ดูแลเซิร์ฟเวอร์ Contabo",
        "content": "ดูแล server ทุกตัว เป็นงานประจำไม่มีวันจบ",
        "expected_category": "areas",
        "expected_deadline": None,
    },
    {
        "title": "สูตรผัดกะเพรา",
        "content": "พริกขี้หนู กระเพรา หมูสับ ซีอิ๊วขาว",
        "expected_category": "resources",
        "expected_deadline": None,
    },
]
```

---

## 17. Acceptance Criteria

### Phase 1 (Core + Web UI)
- [ ] Can create note via API → LLM classifies → saved to DB
- [ ] Web UI shows PARA kanban with notes
- [ ] Can search notes
- [ ] Can edit/delete notes via Web UI
- [ ] Stats page shows counts

### Phase 2 (Hermes MCP + Telegram)
- [ ] MCP server starts and responds to all 10 tools
- [ ] Hermes can call `para_add_note` and get response
- [ ] Telegram webhook receives messages
- [ ] Telegram commands work (/note, /list, /search, /deadlines, /done, /stats)
- [ ] Scheduler runs deadline check → sends Telegram notification

### Phase 3 (Cron + Polish)
- [ ] Cron webhook accepts Hermes cron output as notes
- [ ] Weekly digest sends to Telegram
- [ ] Auto-archive works (completed > 30 days)
- [ ] Links between notes suggested by LLM
- [ ] Export works (markdown + JSON)
- [ ] Docker container builds and runs
- [ ] systemd service file works
- [ ] All tests pass
- [ ] README is complete
- [ ] Git repo pushed to GitHub

---

## 18. Notes for Implementing Agents

### 18.1 LLM API Details

- **Base URL:** `https://ollama.com/v1`
- **Auth:** `Bearer <OLLAMA_API_KEY>`
- **Endpoint:** `POST /chat/completions`
- **Format:** OpenAI-compatible
- **JSON mode:** Pass `"format": "json"` in request body
- **Primary model:** `deepseek-v4-flash`
- **Fallback model:** `gpt-oss:20b`
- **Timeout:** 60 seconds
- **Max retries:** 2

### 18.2 Thai Language Handling

- Notes are primarily in Thai
- LLM prompt is in English (for reliability)
- LLM reasoning should be in Thai
- Tags can be mixed Thai/English
- Deadline extraction must handle Thai dates: "15 สิงหาคม 2025", "31 ธ.ค.", "สิ้นปี"

### 18.3 Error Handling

- LLM failure → fallback model → default to "inbox" category
- Telegram API failure → log error, retry 3 times with backoff
- DB errors → return 500 with JSON error message
- Invalid input → return 422 with validation errors

### 18.4 Security

- API key auth for POST endpoints (`Authorization: Bearer <key>`)
- Telegram webhook validates `X-Telegram-Bot-Api-Secret-Token` header
- Cron webhook validates `Authorization: Bearer <key>`
- No CORS (same-origin only)
- `.env` file gitignored

### 18.5 Performance

- SQLite WAL mode enabled
- Connection pooling via `aiosqlite`
- LLM calls are async (httpx)
- Scheduler runs in background task
- Web UI uses HTMX (no client-side state)

---

## 19. Hermes Cron Integration

### 19.1 How It Works

Hermes cron jobs produce text output. That output can be sent to PARA as a note:

```
Hermes cron job runs → produces text output
→ POST http://localhost:8731/api/notes/cron
→ PARA saves as note with source="cron:<job_name>"
→ LLM classifies automatically
```

### 19.2 Example Hermes Cron Config

```yaml
# In Hermes cron job configuration
- name: server-health-check
  schedule: "0 9 * * *"
  prompt: "เช็คสุขภาพ server ทุกตัว สรุปเป็น text"
  deliver:
    type: webhook
    url: "http://localhost:8731/api/notes/cron"
    headers:
      Authorization: "Bearer <PARA_SECRET_KEY>"
    body_template: |
      {
        "content": "{{output}}",
        "source": "cron:server-health-check",
        "auto_classify": true
      }
```

### 19.3 Cron Webhook Endpoint

```python
@router.post("/api/notes/cron")
async def create_note_from_cron(request: Request):
    """Receive note from Hermes cron job."""
    # Validate API key
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {settings.PARA_SECRET_KEY}":
        raise HTTPException(401, "Unauthorized")

    data = await request.json()
    note = await create_note(
        title=data.get("title", data["source"]),
        content=data["content"],
        source=data["source"],
        auto_classify=data.get("auto_classify", True),
        tags_override=data.get("tags_override", []),
    )
    return note
```

---

## 20. Future Enhancements (Out of Scope for v1)

- Semantic search (embeddings)
- LINE integration
- Note attachments (images, files)
- Collaborative notes (multi-user)
- Mobile app
- Calendar view
- Recurring deadlines
- Note templates
- Voice notes (Whisper transcription)
- MCP server auto-discovery
- Webhooks for external integrations