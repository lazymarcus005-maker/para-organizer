# AGENT SKILL — PARA Organizer Integration

> คู่มือให้ AI agent อื่น (Hermes, Claude Code, Codex, OpenCode ฯลฯ) เชื่อมต่อและเรียกใช้ระบบ **PARA Organizer** ได้ถูกต้อง — ผ่าน **REST API** หรือ **MCP server**
>
> PARA = Projects / Areas / Resources / Archives (+ Inbox) — ระบบ second-brain สำหรับจัดระเบียบโน้ตอัตโนมัติด้วย LLM

---

## 1. ภาพรวม (Overview)

PARA Organizer เปิด 4 ช่องทางให้ agent ใช้งาน:

| ช่องทาง | เหมาะกับ | Auth |
|---------|----------|------|
| **MCP HTTP SSE** (production, `mcp_server_http.py`) | Agent เรียก 27 `para_*` tools ผ่าน HTTP SSE — ช่องทางหลักที่แนะนำ | env vars (ไม่ต้อง token) |
| **REST API** (FastAPI) | HTTP client, integration ทั่วไป, cross-language | Bearer token (บาง endpoint) |
| **MCP stdio** (`mcp_server.py`, local dev เท่านั้น) | รัน local เพื่อ dev/debug — **ไม่ใช้ใน production** | env vars (ไม่ต้อง token) |
| **Telegram bot** | คุยกับมนุษย์ (chat mode / commands) | allowed user IDs |

- **Production:** branch `main`, PostgreSQL v5, deploy บน Dokploy (self-hosted PaaS) — SQLite version เก่าเก็บไว้แค่ที่ branch `backup/sqlite-version` เพื่ออ้างอิงประวัติ ไม่ได้ deploy ที่ไหนแล้ว
- **Base URL (REST, production):** `https://para.mxlabs.cloud` — local dev default `http://localhost:8731` (config `PARA_PORT`)
- **Base URL (MCP HTTP SSE, production):** `https://mc-para.mxlabs.cloud/mcp/sse`
- **DB:** PostgreSQL 16 + pgvector ที่ `169.58.65.88:5436/paradb` (config `PARA_DB_URL`) — full-text search ใช้ PostgreSQL `tsvector`, embeddings ใช้ `pgvector` (ไม่ใช่ SQLite/FTS5/sqlite-vec)
- **LLM:** Ollama Cloud — primary `deepseek-v4-flash`, fallback `gpt-oss:20b`; embeddings ใช้ `nomic-embed-text`

---

## 2. Authentication

Endpoint ที่ **เขียนข้อมูล / sensitive** ต้องแนบ header:

```
Authorization: Bearer <PARA_SECRET_KEY>
```

- ค่า `PARA_SECRET_KEY` มาจาก env / `.env` (default `change-me-in-production` — **เปลี่ยนใน prod เสมอ**)
- ใช้ `hmac.compare_digest` เทียบ (constant-time, กัน timing attack)
- ถ้า token ผิด/ไม่มี → `401 Unauthorized`

**Endpoints ที่ต้องใช้ token** (`Depends(require_api_key)`):
`POST /api/notes`, `PUT /api/settings`, `POST /api/import`, `POST|GET|DELETE /api/backup*`, `POST /api/notes/cron`

Endpoint อ่านข้อมูล (`GET /api/notes`, `/api/search`, `/api/stats` ฯลฯ) **ไม่ต้องใช้ token**

---

## 3. REST API Reference

### 3.1 Notes CRUD

#### `POST /api/notes` 🔒
สร้างโน้ตใหม่ (auto-classify ด้วย LLM ตาม default)

**Request body** (`NoteCreate`):
```json
{
  "title": "string (required, min 1)",
  "content": "string (required, min 1)",
  "source": "manual",
  "auto_classify": true,
  "tags_override": null
}
```
- `auto_classify: true` → LLM จัด `para_category`, `priority`, `deadline`, `tags`, `confidence`
- `tags_override` → ถ้าใส่ array จะข้าม LLM tags ใช้ค่านี้แทน

**Response** `200` → full `Note` object (ดู §5)

```bash
curl -X POST http://localhost:8731/api/notes \
  -H "Authorization: Bearer $PARA_SECRET_KEY" \
  -H "Content-Type: application/json" \
  -d '{"title":"ทำ deck ลูกค้า","content":"ส่งภายใน 30 ก.ค.","auto_classify":true}'
```

#### `GET /api/notes`
รายการโน้ต + pagination

**Query params:** `category`, `status`, `source`, `limit` (default 20, ≤200), `offset` (≥0)

**Response:**
```json
{ "notes": [Note, ...], "total": 42, "limit": 20, "offset": 0 }
```

#### `GET /api/notes/{note_id}`
ดึงโน้ตเดียว → `Note` (หรือ `404`)

#### `PUT /api/notes/{note_id}`
แก้ไขโน้ต (partial update — ส่งเฉพาะ field ที่แก้)

**Body** (`NoteUpdate`, ทุก field optional): `title`, `content`, `para_category`, `sub_category`, `status`, `priority`, `deadline`, `tags`
- validate: `para_category`/`status`/`priority` ต้องเป็นค่าที่ถูกต้อง (ดู §5) ไม่งั้น `422`
- NOT NULL fields (`title`, `content`, `para_category`, `status`, `priority`) ห้ามเป็น null → `422`

#### `DELETE /api/notes/{note_id}`
ลบถาวร → `{ "deleted": <id> }`

#### `POST /api/notes/{note_id}/move`
ย้ายหมวด → body: `{ "para_category": "projects" }` (ต้องอยู่ใน projects/areas/resources/archives)

#### `POST /api/notes/{note_id}/archive`
Archive โน้ต (set `status=archived`, `para_category=archives`, `archived_at=now`)

#### `POST /api/classify/{note_id}`
สั่ง re-classify ด้วย LLM ใหม่ → อัปเดต category/priority/tags/confidence

---

### 3.2 Search

#### `GET /api/search?q=<คำค้น>&limit=20`
Full-text search (PostgreSQL `tsvector`, `ts_rank` ranking)
- `limit` ≤ 100
- token แต่ละคำแปลงเป็น prefix-OR match (`"word"*`)
- snippet ถูก HTML-escape กัน XSS แล้ว swap เป็น `<mark>`

**Response:**
```json
{ "results": [{"id":1,"title":"...","snippet":"...<mark>คำ</mark>...","para_category":"projects","priority":"high","tags":["x"],"rank":-1.2}], "total": 3 }
```

#### `GET /api/search/suggest?q=<คำ>&limit=5`
Autocomplete แบบเบา → `{ "suggestions": [{"id","title","para_category"}] }`

---

### 3.3 Stats / Deadlines / Digest

#### `GET /api/stats`
```json
{ "total_notes": 42, "by_category": {...}, "by_status": {...},
  "by_priority": {...}, "upcoming_deadlines": 5, "avg_confidence": 0.87 }
```

#### `GET /api/deadlines?days=14`
Deadline ที่ยังไม่เลย ภายใน N วัน (`days` 1–365)
```json
{ "deadlines": [{"id","title","deadline":"2026-07-30","days_left":4,"priority":"high"}] }
```

#### `GET /api/digest`
สรุปรายสัปดาห์: `total_notes`, `by_category`, `completed_this_week[]`, `active_projects[]`, `new_notes_count`

---

### 3.4 Cron Webhook (สำหรับ Hermes cron jobs)

#### `POST /api/notes/cron` 🔒
ให้ Hermes cron job ยิงโน้ตอัตโนมัติเข้า PARA

**Body** (`CronNoteCreate`):
```json
{
  "title": "optional (ถ้าไม่ใส่ใช้ source)",
  "content": "string (required)",
  "source": "cron:<job_name>",   // ⚠️ ต้องขึ้นต้นด้วย "cron:" ไม่งั้น 422
  "auto_classify": true,
  "tags_override": null
}
```

```bash
curl -X POST http://localhost:8731/api/notes/cron \
  -H "Authorization: Bearer $PARA_SECRET_KEY" \
  -H "Content-Type: application/json" \
  -d '{"content":"สรุปข่าว AI วันนี้...","source":"cron:daily-news","auto_classify":true}'
```

---

### 3.5 Settings

#### `GET /api/settings`
อ่าน runtime settings ปัจจุบัน

#### `PUT /api/settings` 🔒
แก้ settings (persist ใน DB + apply ทันทีไม่ต้อง restart)

**Keys ที่แก้ได้:** `NOTIFY_DEADLINE_DAYS`, `NOTIFY_DIGEST_DAY`, `NOTIFY_DIGEST_TIME`, `NOTIFY_STALE_DAYS`, `AUTO_ARCHIVE_DAYS`, `RECLASSIFY_INTERVAL_HOURS`, `RECLASSIFY_CONFIDENCE_THRESHOLD`, `CHAT_MODEL`, `CHAT_HISTORY_MAX`, `CHAT_SYSTEM_PROMPT`
- ส่ง key ที่ไม่รู้จัก → `422`
- ค่าไม่ผ่าน validation (เช่น `CHAT_HISTORY_MAX <= 0`) → `422`

---

### 3.6 Backup / Import / Export 🔒

| Endpoint | ทำอะไร |
|----------|--------|
| `POST /api/backup` | สร้าง backup |
| `GET /api/backup` | list backups |
| `POST /api/backup/restore/{filename}` | restore |
| `DELETE /api/backup/{filename}` | ลบ backup |
| `GET /api/backup/download/{filename}` | ดาวน์โหลด |
| `POST /api/import` | import (รับ `application/json`, รองรับ `; charset=utf-8`) |
| `GET /api/export` / `GET /api/export/download` | export JSON |

---

### 3.7 Health Check

| Endpoint | ทำอะไร |
|----------|--------|
| `GET /api/health/live` | liveness probe |
| `GET /api/health/ready` | readiness probe (เช็ค DB connection ฯลฯ) |

---

## 4. MCP Server

Agent เรียก PARA ผ่าน MCP tools ได้ 2 ช่องทาง — **ให้ใช้ HTTP SSE เป็นค่าเริ่มต้น** ส่วน stdio มีไว้สำหรับ local dev เท่านั้น:

### 4.1 ช่องทางหลัก: MCP HTTP SSE (production)
```yaml
mcp:
  servers:
    para-organizer:
      url: "https://mc-para.mxlabs.cloud/mcp/sse"
```
> Implementation: `app/mcp/mcp_server_http.py` — PostgreSQL-native, async SQLAlchemy ผ่าน `app/models_v2.py` โดยใช้ `app/database_v2.py` ของ `async_session_factory`
> ใช้ HTTP SSE transport — connection pooling, scale-out ได้, ไม่ต้อง spawn process ใหม่ทุก connection

### 4.2 MCP stdio — local development เท่านั้น (ไม่ใช่ production)
```yaml
mcp:
  servers:
    para-organizer:
      command: python3
      args: ["app/mcp/mcp_server.py"]
      env:
        PARA_DB_URL: postgresql+asyncpg://user:pass@169.58.65.88:5436/paradb
        OLLAMA_API_KEY: ${OLLAMA_API_KEY}
```
> รัน `cwd` ที่ repo root หรือใส่ absolute path ใน args — ใช้สำหรับ dev/debug local เท่านั้น ห้ามใช้ชี้ production

### 4.3 MCP Tools ที่มี (27 tools)

ทั้ง `mcp_server.py` (stdio, local dev) และ `mcp_server_http.py` (HTTP SSE, production) เปิด tool set เดียวกัน:

| Tool | Signature | ทำอะไร |
|------|-----------|--------|
| `para_add_note` | `(title: str, content: str) -> dict` | สร้างโน้ต + auto-classify |
| `para_search` | `(query: str, category=None, limit=10) -> list` | full-text search |
| `para_list` | `(category=None, status=None, limit=20) -> list` | list โน้ต |
| `para_get` | `(id: int) -> dict` | ดึงโน้ตเดียว |
| `para_move` | `(id: int, category: str) -> dict` | ย้ายหมวด |
| `para_archive` | `(id: int) -> dict` | archive |
| `para_stats` | `() -> dict` | สถิติรวม |
| `para_deadlines` | `(days_ahead=14) -> list` | deadline ใกล้ถึง |
| `para_digest` | `() -> dict` | สรุปสัปดาห์ |
| `para_add_link` | `(from_id, to_id, link_type="related") -> dict` | เชื่อมโน้ต (related/depends_on/refines) |
| `para_update` | `(id: int, ...) -> dict` | แก้ไขโน้ต (partial update) |
| `para_complete` | `(id: int) -> dict` | ทำเครื่องหมายโน้ตว่าเสร็จแล้ว |
| `para_delete` | `(id: int) -> dict` | ลบโน้ตถาวร |
| `para_reclassify` | `(id: int) -> dict` | สั่ง re-classify ด้วย LLM ใหม่ |
| `para_ask` | `(question: str) -> dict` | ถามคำถามเกี่ยวกับ second-brain (RAG-style) |
| `para_context` | `(id: int) -> dict` | ดึง context รอบโน้ต |
| `para_create_task` | `(...) -> dict` | มอบหมาย background task |
| `para_task_result` | `(task_id) -> dict` | รายงานผลลัพธ์ของ background task |
| `para_tasks` | `(...) -> list` | list background tasks |
| `para_brain_state` | `() -> dict` | ดึงสรุปสถานะ second-brain ปัจจุบัน |
| `para_graph_context` | `(id: int) -> dict` | ดึง graph neighborhood รอบโน้ต |
| `para_related` | `(id: int) -> list` | ดึงโน้ตที่เกี่ยวข้องกับโน้ตนี้ |
| `para_items` | `(note_id: int) -> list` | list checklist items ของโน้ต |
| `para_add_item` | `(note_id: int, text: str) -> dict` | เพิ่ม checklist item |
| `para_done_item` | `(item_id: int) -> dict` | ทำเครื่องหมาย checklist item ว่าเสร็จ |
| `para_plan` | `(horizon: str) -> dict` | สร้าง action plan ตามช่วงเวลา (horizon) |
| `para_feedback_stats` | `() -> dict` | ดึงสถิติ feedback/ความแม่นยำของ classification |

- ทุก tool คืน dict/list ที่ JSON-serialize ได้
- error (not found / invalid) คืน `{"error": "..."}` — server ไม่ crash

### 4.4 ตัวอย่าง (natural language ผ่าน Hermes)
```
"เพิ่มโน้ตลง PARA: ประชุมทีม พรุ่งนี้ 10 โมง"   → para_add_note
"หาโน้ตเรื่อง deadline ในโปรเจกต์"              → para_search
"โปรเจกต์ไหนใกล้ครบกำหนดบ้าง"                  → para_deadlines
```

---

## 5. Data Models (reference)

### Note (response object)
```json
{
  "id": 1,
  "title": "string",
  "content": "string",
  "para_category": "inbox|projects|areas|resources|archives",
  "sub_category": "string|null",
  "status": "active|completed|archived",
  "priority": "low|medium|high|urgent",
  "deadline": "YYYY-MM-DD|null",
  "tags": ["..."],
  "source": "manual|telegram|cron:<job>|...",
  "source_metadata": {},
  "llm_model": "string|null",
  "llm_confidence": 0.0,
  "llm_reasoning": "string|null",
  "created_at": "ISO datetime",
  "updated_at": "ISO datetime",
  "archived_at": "ISO datetime|null"
}
```

### Enums
| Field | ค่าที่ยอมรับ |
|-------|-------------|
| `para_category` | `inbox`, `projects`, `areas`, `resources`, `archives` |
| `status` | `active`, `completed`, `archived` |
| `priority` | `low`, `medium`, `high`, `urgent` |
| `link_type` (MCP) | `related`, `depends_on`, `refines` |

**PARA meaning:**
- **projects** — งาน active มี deadline/goal ชัดเจน
- **areas** — ความรับผิดชอบต่อเนื่อง ไม่มีวันจบ
- **resources** — reference material ไม่ต้อง action
- **archives** — เสร็จแล้ว/ไม่เกี่ยวข้องแล้ว
- **inbox** — ยังไม่จัด (default เมื่อ LLM fail)

---

## 6. Error Handling

| Status | ความหมาย | รับมือ |
|--------|----------|--------|
| `401` | token ผิด/ไม่มี | ตรวจ `Authorization: Bearer <PARA_SECRET_KEY>` |
| `404` | ไม่พบโน้ต | ตรวจ note_id |
| `422` | validation fail | ตรวจ enum / required field / source prefix |

- LLM classify fail → โน้ตถูกวางใน `inbox` พร้อม `confidence: 0.0` (ไม่ error)
- FTS query ว่าง/แปลก → คืน `results: []` ไม่ 500

---

## 7. Quick Integration Checklist

- [ ] ตั้ง `PARA_SECRET_KEY` และแนบ Bearer token ทุก write endpoint
- [ ] เขียนโน้ตอัตโนมัติจาก cron → ใช้ `POST /api/notes/cron` + `source: "cron:<name>"`
- [ ] Agent ทั่วไป → ต่อ MCP HTTP SSE ที่ `https://mc-para.mxlabs.cloud/mcp/sse` แล้วเรียก `para_*` tools (ไม่ต้อง token) — ใช้ stdio เฉพาะ local dev เท่านั้น
- [ ] ค้นข้อมูลก่อนตอบผู้ใช้ → `GET /api/search` หรือ `para_search`
- [ ] เช็คงานใกล้ deadline → `GET /api/deadlines` หรือ `para_deadlines`
- [ ] เช็ค service พร้อมใช้งาน → `GET /api/health/live`, `GET /api/health/ready`
- [ ] อย่า hardcode enum — ยึดตาม §5
- [ ] จัดการ `401/404/422` ให้ครบ

---

*Generated for agent-to-agent integration. อ้างอิงจาก source จริงใน `app/routes/`, `app/mcp/mcp_server_http.py` (production), `app/mcp/mcp_server.py` (local dev), `app/models_v2.py`*
