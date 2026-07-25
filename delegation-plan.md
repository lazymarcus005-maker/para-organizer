# PARA Organizer — Delegation Plan

> **Date:** 2025-07-25 | **Prepared for:** Marcus | **Workdir:** ~/workspace/PARA-organizer/

## Overview

3 coding agents working in parallel on different phases. Each gets a self-contained prompt that references `spec.md`.

```
Claude Code (opus-4.8)     → Phase 1: Core + Web UI
OpenCode (qwen3.8-preview) → Phase 2: MCP Server + Hermes Integration  
Codex (gpt5.6-sole)        → Phase 3: Telegram Bot + Scheduler + Notifier
```

**IMPORTANT:** All 3 work on separate branches. Merge after all complete.

---

## Pre-flight Setup (run before delegating)

```bash
cd ~/workspace/PARA-organizer

# Init git
git init
git add spec.md
git commit -m "Add spec document"

# Create 3 branches
git checkout -b phase1-core
git checkout main
git checkout -b phase2-mcp
git checkout main
git checkout -b phase3-telegram

# Create .env.example
cat > .env.example << 'EOF'
PARA_PORT=8731
PARA_DB_PATH=/var/lib/para-organizer/data/para.db
PARA_SECRET_KEY=change-me

OLLAMA_API_KEY=<your-key>
OLLAMA_BASE_URL=https://ollama.com/v1
LLM_PRIMARY=deepseek-v4-flash
LLM_FALLBACK=gpt-oss:20b
LLM_TIMEOUT=60

TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_WEBHOOK_URL=https://para.mxlabs.cloud/webhook/telegram
TELEGRAM_ALLOWED_USERS=<user-ids>

NOTIFY_CHANNEL=telegram
NOTIFY_DEADLINE_DAYS=7,3,1
NOTIFY_DIGEST_DAY=mon
NOTIFY_DIGEST_TIME=08:00
NOTIFY_STALE_DAYS=14

AUTO_ARCHIVE_DAYS=30
RECLASSIFY_INTERVAL_HOURS=6
RECLASSIFY_CONFIDENCE_THRESHOLD=0.7
EOF

git add .env.example
git commit -m "Add .env.example"
git push -u origin main
```

---

## Agent 1: Claude Code (opus-4.8) — Phase 1: Core + Web UI

### Branch: `phase1-core`

### Prompt (copy-paste to Claude Code)

```
You are working on the PARA Organizer project. Read the spec at ~/workspace/PARA-organizer/spec.md thoroughly before starting.

Work directory: ~/workspace/PARA-organizer/
Branch: phase1-core

Your task: Implement Phase 1 — Core Backend + Web UI

Deliverables:
1. app/config.py — Pydantic settings loading from .env
2. app/database.py — SQLite connection with WAL mode, auto-migrate on startup
3. app/models.py — Pydantic models for Note, Link, History, Notification, Stats
4. app/classifier.py — LLM classifier calling Ollama Cloud (deepseek-v4-flash primary, gpt-oss:20b fallback). Use the exact prompt from spec section 6.1. Handle JSON parsing, validation, fallback, and default-to-inbox on failure.
5. app/routes/notes.py — Full CRUD: POST /api/notes, GET /api/notes, GET /api/notes/{id}, PUT, DELETE, POST /api/notes/{id}/move, POST /api/notes/{id}/archive, POST /api/classify/{id}
6. app/routes/para.py — GET /api/para/tree (returns counts per category)
7. app/routes/search.py — GET /api/search?q= using FTS5
8. app/routes/stats.py — GET /api/stats, GET /api/deadlines?days=N, GET /api/digest
9. app/routes/export.py — GET /api/export?format=md|json
10. app/routes/pages.py — Web UI pages (Jinja2)
11. app/templates/ — base.html, index.html (PARA kanban 4 columns), note_detail.html, note_new.html, stats.html, search.html
12. app/main.py — FastAPI app, include all routers, startup event (init DB + migrations), CORS disabled
13. requirements.txt — all dependencies from spec section 13
14. scripts/init_db.py — initialize database with schema from spec section 5
15. scripts/seed.py — seed 5 test notes (Thai content) for manual testing
16. .gitignore — data/, .env, __pycache__/, .venv/

Web UI requirements:
- Tailwind CSS via CDN (no build step)
- HTMX for interactions (add note, move note, search)
- PARA kanban: 4 columns (Projects, Areas, Resources, Archives)
- Note cards show: title, priority dot (🔴🟡🟢), deadline badge, tags
- Search bar at top
- Stats bar at bottom: total counts
- New note form: title + content textarea, submit → auto-classify → redirect to kanban

Database requirements:
- All tables from spec section 5 (notes, links, history, notifications, settings)
- FTS5 virtual table + triggers for notes
- WAL mode enabled
- Auto-migrate on app startup (CREATE TABLE IF NOT EXISTS)

LLM classifier requirements:
- Async httpx calls to Ollama Cloud
- JSON format mode
- 60s timeout, 2 retries
- Primary: deepseek-v4-flash, Fallback: gpt-oss:20b
- On all-fail: default to "inbox" with confidence 0.0
- Store llm_model, llm_confidence, llm_reasoning in DB

Do NOT implement: MCP server, Telegram bot, scheduler, notifier, cron webhook, Docker, systemd, tests. Those are other agents' tasks.

After completing all files:
1. Run: python3 scripts/init_db.py (verify DB creates successfully)
2. Run: python3 -m uvicorn app.main:app --port 8731 (verify server starts)
3. Test: curl http://localhost:8731/api/stats (should return JSON)
4. git add -A && git commit -m "Phase 1: Core backend + Web UI"

Constraints:
- Python 3.12+
- Use python3 not python
- No external database (SQLite only)
- No Node.js / npm (Tailwind via CDN)
- Keep it simple, no over-engineering
- Thai text must work correctly in DB and UI
```

---

## Agent 2: OpenCode (qwen3.8-preview) — Phase 2: MCP Server

### Branch: `phase2-mcp`

### Prompt (copy-paste to OpenCode)

```
You are working on the PARA Organizer project. Read the spec at ~/workspace/PARA-organizer/spec.md thoroughly before starting.

Work directory: ~/workspace/PARA-organizer/
Branch: phase2-mcp

Your task: Implement Phase 2 — MCP Server for Hermes Integration

Deliverables:
1. app/mcp/mcp_server.py — MCP server with 10 tools (stdio transport)
2. app/mcp/__init__.py
3. tests/test_mcp.py — unit tests for MCP tools (mock DB)

MCP Tools to implement (spec section 8.2):
- para_add_note(title: str, content: str) -> Note
- para_search(query: str, category: str = None, limit: int = 10) -> Note[]
- para_list(category: str = None, status: str = None, limit: int = 20) -> Note[]
- para_get(id: int) -> Note
- para_move(id: int, category: str) -> Note
- para_archive(id: int) -> Note
- para_stats() -> Stats
- para_deadlines(days_ahead: int = 14) -> Deadline[]
- para_digest() -> Digest
- para_add_link(from_id: int, to_id: int, link_type: str = "related") -> Link

Implementation requirements:
- Use the `mcp` Python SDK (pip install mcp)
- stdio transport (Hermes launches this as a subprocess)
- Each tool connects to SQLite directly (read PARA_DB from env)
- para_add_note must call the LLM classifier (import from app.classifier)
- All other tools are read/write to SQLite
- Return JSON-serializable dicts
- Handle errors gracefully (return error message, don't crash)

The MCP server must be launchable as:
  python3 app/mcp/mcp_server.py

And configurable in Hermes config.yaml as:
  mcp:
    servers:
      para-organizer:
        command: python3
        args: ["app/mcp/mcp_server.py"]
        env:
          PARA_DB: /var/lib/para-organizer/data/para.db
          OLLAMA_API_KEY: ${OLLAMA_API_KEY}

Dependencies to add to requirements.txt:
  mcp>=1.0.0

Test requirements:
- tests/test_mcp.py with pytest
- Mock the database (use in-memory SQLite)
- Test each tool's input/output
- Test error cases (note not found, invalid category)

Do NOT implement: REST API, Web UI, Telegram, scheduler, notifier, Docker. Those are other agents' tasks.

IMPORTANT: You depend on Phase 1's app/database.py, app/models.py, app/classifier.py. If those files don't exist yet, create minimal stubs that match the spec so your MCP server can be tested independently. The stubs will be replaced when branches merge.

After completing:
1. pip install mcp (verify install)
2. python3 -m pytest tests/test_mcp.py -v (verify tests pass)
3. git add -A && git commit -m "Phase 2: MCP server + Hermes integration"

Constraints:
- Python 3.12+
- Use python3 not python
- MCP stdio transport only (no HTTP)
- Keep tools simple and focused
- Thai text must work in tool responses
```

---

## Agent 3: Codex (gpt5.6-sole) — Phase 3: Telegram + Scheduler

### Branch: `phase3-telegram`

### Prompt (copy-paste to Codex)

```
You are working on the PARA Organizer project. Read the spec at ~/workspace/PARA-organizer/spec.md thoroughly before starting.

Work directory: ~/workspace/PARA-organizer/
Branch: phase3-telegram

Your task: Implement Phase 3 — Telegram Bot + Scheduler + Notifier + Cron Webhook

Deliverables:
1. app/integrations/telegram_bot.py — Telegram webhook handler + command parser
2. app/integrations/__init__.py
3. app/routes/telegram_webhook.py — POST /webhook/telegram
4. app/routes/cron_webhook.py — POST /api/notes/cron (receive from Hermes cron)
5. app/scheduler.py — APScheduler with 5 cron jobs
6. app/notifier.py — Notification router (→ Telegram)
7. tests/test_telegram.py — webhook + command tests
8. tests/test_scheduler.py — scheduler job tests
9. tests/conftest.py — pytest fixtures (test client, in-memory DB, mock LLM)

Telegram Bot requirements (spec section 9):
- Webhook receiver: POST /webhook/telegram
- Validate X-Telegram-Bot-Api-Secret-Token header (if configured)
- Commands: /note, /list, /list <cat>, /search, /deadlines, /done, /move, /stats, /digest, /help
- Plain text (no command) → treat as new note
- Reply with formatted text:
  ✅ บันทึกแล้ว!
  📂 Projects · 🔴 High
  📅 Deadline: 15 ส.ค. 2025
  🏷️ tag1, tag2
- /list shows inline keyboard buttons for categories
- Use python-telegram-bot library

Scheduler requirements (spec section 10):
- APScheduler AsyncIOScheduler
- 5 jobs:
  1. reclassify_job — every 6 hours (reclassify notes with confidence < 0.7)
  2. auto_archive_job — daily 02:00 (archive completed > 30 days)
  3. deadline_check_job — daily 09:00 (check 7/3/1 day deadlines → notify Telegram)
  4. stale_project_job — daily 18:00 (check projects not updated > 14 days → notify)
  5. weekly_digest_job — Monday 08:00 (generate digest → notify Telegram)
- Scheduler starts in FastAPI startup event
- Scheduler shuts down in FastAPI shutdown event

Notifier requirements (spec section 9.4):
- send_telegram(chat_id, text) — use Telegram Bot API
- notify_deadline(note, days_left) — format message and send
- notify_stale(note) — format message and send
- send_digest(digest_data) — format digest and send
- Retry 3 times with exponential backoff on failure
- Log all send attempts

Cron Webhook requirements (spec section 19):
- POST /api/notes/cron
- Validate Authorization: Bearer <PARA_SECRET_KEY>
- Accept: {content, source, auto_classify, tags_override}
- Create note with source="cron:<job_name>"
- If auto_classify=true → call LLM classifier

Notification message formats (spec section 9.4):
- Deadline reminder: ⏰ ใกล้ถึงกำหนด! + note details + days left
- Weekly digest: 🧠 PARA Weekly Digest + stats + completed + active + stale

Dependencies to add to requirements.txt:
  python-telegram-bot>=21.0
  apscheduler>=3.10.0
  pytest>=8.0.0
  pytest-asyncio>=0.23.0

Test requirements:
- tests/conftest.py: FastAPI TestClient fixture, in-memory SQLite, mock LLM responses
- tests/test_telegram.py: test webhook parsing, each command, plain text → note
- tests/test_scheduler.py: test each job's logic (mock datetime)

Do NOT implement: REST API CRUD, Web UI, MCP server, classifier logic, database schema. Those are other agents' tasks.

IMPORTANT: You depend on Phase 1's app/database.py, app/models.py, app/classifier.py and app/routes/notes.py. If those files don't exist yet, create minimal stubs matching the spec so your code can be tested independently. Stubs will be replaced when branches merge.

After completing:
1. python3 -m pytest tests/ -v (verify all tests pass)
2. git add -A && git commit -m "Phase 3: Telegram bot + scheduler + notifier + cron webhook"

Constraints:
- Python 3.12+
- Use python3 not python
- Telegram webhook only (no long polling in production)
- Scheduler must not block the event loop
- Thai text in messages must render correctly
- Mock external API calls in tests (don't call real Telegram or LLM)
```

---

## Execution Order

```
┌─────────────────────────────────────────────────────┐
│  PRE-FLIGHT (you do this first)                     │
│  • git init + branches                              │
│  • .env.example                                     │
│  • push to GitHub                                   │
└──────────────────────┬──────────────────────────────┘
                       │
       ┌───────────────┼───────────────┐
       ▼               ▼               ▼
┌─────────────┐ ┌─────────────┐ ┌─────────────┐
│  Claude Code │ │  OpenCode   │ │   Codex     │
│  (opus-4.8)  │ │ (qwen3.8)   │ │ (gpt5.6)    │
│              │ │             │ │             │
│  Phase 1     │ │  Phase 2    │ │  Phase 3    │
│  Core + UI   │ │  MCP Server │ │  Telegram   │
│              │ │             │ │  + Sched    │
│  branch:     │ │  branch:    │ │  branch:    │
│  phase1-core │ │  phase2-mcp │ │  phase3-tel │
└──────┬───────┘ └──────┬──────┘ └──────┬──────┘
       │                │               │
       │    (all 3 run in parallel)     │
       │                │               │
       ▼                ▼               ▼
┌─────────────────────────────────────────────────────┐
│  MERGE (you do this after all 3 complete)           │
│  1. git checkout main                               │
│  2. git merge phase1-core                            │
│  3. git merge phase2-mcp                             │
│  4. git merge phase3-telegram                        │
│  5. Resolve conflicts (mainly requirements.txt)      │
│  6. Run full test suite                              │
│  7. python3 scripts/init_db.py                       │
│  8. uvicorn app.main:app --port 8731                 │
│  9. Test all endpoints manually                      │
│  10. git push                                        │
└─────────────────────────────────────────────────────┘
```

---

## Merge Conflict Strategy

Likely conflicts:
1. `requirements.txt` — merge all dependencies
2. `app/main.py` — each agent adds their router includes. Merge all.
3. `app/routes/__init__.py` — merge all router imports

Unlikely conflicts (each agent works on separate files):
- Phase 1: config, database, models, classifier, routes/notes, routes/para, routes/search, routes/stats, routes/export, routes/pages, templates/
- Phase 2: mcp/, tests/test_mcp.py
- Phase 3: integrations/, routes/telegram_webhook, routes/cron_webhook, scheduler, notifier, tests/test_telegram, tests/test_scheduler, tests/conftest

---

## Post-Merge Verification

```bash
# 1. Install all deps
pip install -r requirements.txt

# 2. Init DB
python3 scripts/init_db.py

# 3. Seed test data
python3 scripts/seed.py

# 4. Run tests
python3 -m pytest tests/ -v

# 5. Start server
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8731

# 6. Test endpoints
curl http://localhost:8731/api/stats
curl -X POST http://localhost:8731/api/notes \
  -H "Content-Type: application/json" \
  -d '{"title":"ทดสอบ","content":"ต่อทะเบียนรถ ก่อน 15 ส.ค.","source":"manual"}'
curl http://localhost:8731/api/para/tree
curl "http://localhost:8731/api/search?q=ทะเบียน"
curl http://localhost:8731/api/deadlines?days=30

# 7. Test Web UI
open http://localhost:8731/

# 8. Test MCP server
python3 app/mcp/mcp_server.py  # should start without error

# 9. Test Telegram webhook (if token configured)
curl -X POST http://localhost:8731/webhook/telegram \
  -H "Content-Type: application/json" \
  -d '{"message":{"text":"/note ทดสอบ","chat":{"id":123}}}'

# 10. Push to GitHub
git push origin main
```

---

## Quick Commands for Tomorrow

### Start all 3 agents in parallel (tmux)

```bash
# Claude Code — Phase 1
tmux new-session -d -s para-claude -x 180 -y 50
tmux send-keys -t para-claude 'cd ~/workspace/PARA-organizer && git checkout phase1-core && claude --dangerously-skip-permissions' Enter
sleep 5
tmux send-keys -t para-claude Enter  # trust dialog
sleep 3
tmux send-keys -t para-claude Down && sleep 0.3 && tmux send-keys -t para-claude Enter  # permissions
sleep 3
tmux send-keys -t para-claude 'Read spec.md then implement Phase 1 Core + Web UI. All details in spec.md sections 1-17. Work on branch phase1-core.' Enter

# OpenCode — Phase 2
tmux new-session -d -s para-opencode -x 180 -y 50
tmux send-keys -t para-opencode 'cd ~/workspace/PARA-organizer && git checkout phase2-mcp && opencode' Enter
sleep 5
tmux send-keys -t para-opencode 'Read spec.md then implement Phase 2 MCP Server. All details in spec.md section 8. Work on branch phase2-mcp.' Enter

# Codex — Phase 3
tmux new-session -d -s para-codex -x 180 -y 50
tmux send-keys -t para-codex 'cd ~/workspace/PARA-organizer && git checkout phase3-telegram && codex' Enter
sleep 5
tmux send-keys -t para-codex 'Read spec.md then implement Phase 3 Telegram + Scheduler. All details in spec.md sections 9-10. Work on branch phase3-telegram.' Enter
```

### Monitor all 3

```bash
# Check all sessions
for s in para-claude para-opencode para-codex; do
  echo "=== $s ==="
  tmux capture-pane -t $s -p -S -10
  echo ""
done

# Watch one in real time
tmux attach -t para-claude
```

### Kill all when done

```bash
for s in para-claude para-opencode para-codex; do
  tmux kill-session -t $s
done
```

---

## File Ownership Matrix

| File | Phase 1 (Claude) | Phase 2 (OpenCode) | Phase 3 (Codex) |
|------|:-:|:-:|:-:|
| app/config.py | ✅ | | |
| app/database.py | ✅ | | |
| app/models.py | ✅ | | |
| app/classifier.py | ✅ | | |
| app/main.py | ✅ (base) | + router | + router + scheduler |
| app/routes/notes.py | ✅ | | |
| app/routes/para.py | ✅ | | |
| app/routes/search.py | ✅ | | |
| app/routes/stats.py | ✅ | | |
| app/routes/export.py | ✅ | | |
| app/routes/pages.py | ✅ | | |
| app/routes/telegram_webhook.py | | | ✅ |
| app/routes/cron_webhook.py | | | ✅ |
| app/mcp/mcp_server.py | | ✅ | |
| app/integrations/telegram_bot.py | | | ✅ |
| app/scheduler.py | | | ✅ |
| app/notifier.py | | | ✅ |
| app/templates/* | ✅ | | |
| tests/conftest.py | | | ✅ |
| tests/test_mcp.py | | ✅ | |
| tests/test_telegram.py | | | ✅ |
| tests/test_scheduler.py | | | ✅ |
| requirements.txt | ✅ (base) | + mcp | + telegram, apscheduler, pytest |
| scripts/init_db.py | ✅ | | |
| scripts/seed.py | ✅ | | |
| Dockerfile | (later) | | |
| docker-compose.yml | (later) | | |
| para-organizer.service | (later) | | |
| README.md | (later) | | |