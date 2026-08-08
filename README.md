# PARA Organizer

A self-hosted Second Brain system that auto-classifies notes using an LLM into **P**rojects / **A**reas / **R**esources / **A**rchives (PARA). Integrates with the Hermes Agent (via MCP), a Telegram Bot, and a Web UI.

## Features

- **Auto-classify notes with LLM** (`deepseek-v4-flash`) into Projects / Areas / Resources / Archives
- **Auto-deadline extraction** from note content
- **Auto-tagging** with confidence-based routing (low-confidence notes get flagged for review)
- **Full-text search** (PostgreSQL `tsvector`) **+ semantic search** (`pgvector` embeddings, hybrid RAG)
- **MCP server** with 27 tools for direct Hermes Agent integration, served over HTTP SSE (add/search/list/get/move/archive/update/complete/delete/stats/deadlines/digest/reclassify/ask/context/brain-state/graph/related/items/plan/tasks/feedback and more)
- **Telegram bot** — conversational chat mode, voice message transcription (STT), inline action buttons (snooze/done/keep/archive)
- **8 scheduler jobs** — reclassify, auto-archive, escalation, deadline reminders, stale-note detection, daily/weekly digest, weekly review, embedding backfill
- **Auto-linking** between related notes (graph-based relationships)
- **Note distillation** on archive (LLM summarizes long notes before archiving)
- **Weekly AI review** with 3 actionable recommendations
- **Kanban board**, **graph view**, **cost/usage dashboard**, and **quick-capture UI**
- **Local + cloud backup** (S3-compatible storage)
- **Recurring notes** (spawn new instances on a schedule)
- **Thai language support** throughout classification, chat, and UI

## Quick Start

```bash
cp .env.example .env
# Edit .env with your OLLAMA_API_KEY (and optionally TELEGRAM_BOT_TOKEN)
docker-compose up -d
# Open http://localhost:8731
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full deployment guide, including production setup on Dokploy.

## Architecture

PARA Organizer v5 is the **production system**: a distributed set of Docker services deployed on [Dokploy](https://dokploy.com) (self-hosted PaaS), backed by an external PostgreSQL + pgvector database and Redis. This is a single/two-user deployment, so every service runs at `replicas: 1` — there's no horizontal scaling in front of them.

```text
Internet
    │
    ▼
┌──────────────────────┐
│  Dokploy Traefik      │  ← built-in reverse proxy, Let's Encrypt auto-cert
│  :443                 │
└──────┬────────────────┘
       │
       ├────────────────────────────────────────────┐
       ▼                                            ▼
┌──────────────────────┐              ┌────────────────────────────┐
│  para-app:8731        │              │  para-mcp:8100              │
│  FastAPI REST         │              │  MCP HTTP SSE                │
│  para.mxlabs.cloud    │              │  mc-para.mxlabs.cloud        │
└──────────┬─────────────┘              └──────────────┬───────────────┘
           │                                           │
           └───────────────────┬───────────────────────┘
                                ▼
                  ┌────────────────────────────┐
                  │  PostgreSQL 16 + pgvector    │  ← external host
                  │  169.58.65.88:5436/paradb    │     tsvector + pgvector
                  └────────────────────────────┘
                                ▲
                  ┌────────────────────────────┐
                  │  para-redis (Redis 7)        │  ← task queue + cache
                  └────────────────────────────┘

┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
│  para-worker          │  │  para-scheduler        │  │  para-backup          │
│  Consumes tasks       │  │  APScheduler singleton │  │  Optional cloud backup │
│  from Redis           │  │                        │  │                        │
└──────────────────────┘  └──────────────────────┘  └──────────────────────┘
```

### Services

All services run with `replicas: 1` (see `docker-compose.yml`).

| Service | Role | Public host |
|---------|------|-------------|
| **para-app** | FastAPI REST API, port 8731 | `para.mxlabs.cloud` |
| **para-mcp** | MCP HTTP SSE server, port 8100 | `mc-para.mxlabs.cloud` (SSE endpoint `/mcp/sse`) |
| **para-worker** | Background task consumer (Redis queue) | — |
| **para-scheduler** | APScheduler singleton (reclassify, digests, deadlines, etc.) | — |
| **para-backup** | Optional cloud backup job | — |
| **para-redis** | Redis 7 — task queue + cache | — |

The PostgreSQL 16 + pgvector database is **not** part of this compose stack — it's an externally managed host (`169.58.65.88:5436/paradb`) referenced via `PARA_DB_URL`.

### Key Changes from v4

- **SQLite → PostgreSQL**: Concurrent writes, `pgvector` for embeddings, `tsvector` for full-text search — this is now THE production database
- **In-process scheduler → Standalone**: APScheduler runs in its own container (`para-scheduler`)
- **MCP stdio → HTTP SSE**: `para-mcp` is a standalone HTTP SSE service; the old stdio MCP server (`app/mcp/mcp_server.py`) is legacy/local-dev only
- **No cache → Redis cache**: Read-heavy endpoints cached with configurable TTL
- **Background tasks → Redis task queue**: Durable, retryable, observable, consumed by `para-worker`
- **host network → overlay network**: Docker Swarm-compatible, deployed via Dokploy

The old SQLite version (v4) is preserved for historical reference on the `backup/sqlite-version` branch and is not deployed anywhere.

## Tech Stack

| Layer | Technology |
|---|---|
| API server | FastAPI + Uvicorn |
| Database | **PostgreSQL 16 + pgvector** (production) — SQLite (legacy v4, see `backup/sqlite-version`) |
| LLM | Ollama Cloud (`deepseek-v4-flash` primary, `gpt-oss:20b` fallback/chat) |
| Embeddings | `nomic-embed-text` via Ollama |
| Scheduler | APScheduler, standalone singleton (`para-scheduler`) |
| Task Queue | Redis 7 (`para-redis`) |
| Cache | Redis 7 (`para-redis`) |
| Bot | python-telegram-bot |
| Agent integration | **MCP HTTP SSE** (production) — MCP stdio (local-dev only) |
| Templating | Jinja2 |
| Containerization | Docker + docker-compose, deployed via Dokploy |
| Testing | pytest + pytest-asyncio |

## Deployment

This project uses **GitHub Flow**: create a feature branch → open a PR → merge to `main`. Dokploy watches `main` and deploys on merge — there is no separate manual release step.

**Deployment process:**

1. Create a feature branch from `main`
2. Implement changes
3. Open a Pull Request to `main`
4. Review and test
5. Merge the PR to `main` → Dokploy pulls and redeploys

**Production URLs:**
- REST API: https://para.mxlabs.cloud
- MCP HTTP SSE: https://mc-para.mxlabs.cloud/mcp/sse

For deployment steps and the full guide, see [DEPLOYMENT.md](DEPLOYMENT.md).

## Project Structure

```
para-organizer/
├── app/
│   ├── main.py              # FastAPI app entrypoint
│   ├── config.py            # Settings (env-driven)
│   ├── database_v2.py       # PostgreSQL async engine/session (production)
│   ├── models_v2.py         # PostgreSQL SQLAlchemy models (production)
│   ├── database.py          # SQLite connection, FTS5/sqlite-vec (legacy v4, see backup/sqlite-version)
│   ├── models.py             # PARA categories, legacy SQLite data models
│   ├── classifier.py        # LLM classification, tagging, deadline extraction
│   ├── chat.py               # Conversational / RAG chat mode
│   ├── scheduler.py         # APScheduler job definitions
│   ├── embed.py              # Embedding generation for semantic search
│   ├── linker.py             # Auto-linking between related notes
│   ├── distill.py            # Note distillation on archive
│   ├── review.py             # Weekly AI review
│   ├── planner.py            # Action item planning
│   ├── graph.py              # Note relationship graph
│   ├── notifier.py           # Notification dispatch (Telegram, digests)
│   ├── mcp/mcp_server_http.py # MCP HTTP SSE server (27 tools) — production
│   ├── mcp/mcp_server.py     # MCP stdio server — local-dev only
│   ├── integrations/         # Telegram bot, etc.
│   ├── routes/               # FastAPI routers (notes, search, backup, graph, ...)
│   ├── templates/            # Jinja2 templates (Web UI)
│   └── static/                # CSS/JS assets
├── tests/                    # pytest test suite
├── scripts/                   # DB init and maintenance scripts
├── docker-compose.yml
├── Dockerfile
├── DEPLOYMENT.md              # Full deployment guide
├── AGENTS.md                  # AI coding agent project context
└── agent_skill.md             # MCP/REST API integration guide for external agents
```

## Environment Variables

Key variables (see `.env.example` for the full list):

| Variable | Purpose |
|---|---|
| `PARA_DB_URL` | PostgreSQL connection string (`postgresql+asyncpg://...`) — production database |
| `PARA_REDIS_URL` | Redis connection string — task queue + cache |
| `PARA_DB_PATH` | *Legacy.* SQLite database file path, used only by the v4 code path on `backup/sqlite-version` |
| `PARA_PORT` | Port the app listens on (default `8731`) |
| `PARA_SECRET_KEY` | Bearer token for authenticated API endpoints |
| `OLLAMA_API_KEY` | Ollama Cloud API key (required for classification) |
| `LLM_PRIMARY` / `LLM_FALLBACK` | Classification models (`deepseek-v4-flash` / `gpt-oss:20b`) |
| `CHAT_MODEL` | Model used for conversational chat |
| `EMBED_PROVIDER` / `EMBED_MODEL` | Embedding provider/model for semantic search (pgvector) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (optional) |
| `TELEGRAM_ALLOWED_USERS` | Comma-separated allowed Telegram user IDs |
| `AUTO_ARCHIVE_DAYS` | Days of inactivity before auto-archiving a note |
| `WEB_PUBLIC_URL` | Public base URL (used for webhooks, links) — `https://para.mxlabs.cloud` in production |

See `.env.example` for the full list, and [DEPLOYMENT.md](DEPLOYMENT.md) for per-service environment variables.

## API Overview

PARA Organizer exposes a **REST API** (FastAPI, see `app/routes/`) at `https://para.mxlabs.cloud` for the Web UI and general HTTP integrations, and an **MCP HTTP SSE server** (`app/mcp/mcp_server_http.py`) at `https://mc-para.mxlabs.cloud/mcp/sse` exposing 27 tools for direct use by Hermes or other MCP-compatible agents. See [agent_skill.md](agent_skill.md) for the full integration guide covering both channels, authentication, and request/response formats.

## Contributing

1. Create a feature branch from `main`
2. Make your changes and add/update tests
3. Open a Pull Request to `main`
4. After review and passing tests, merge — this deploys automatically to production
