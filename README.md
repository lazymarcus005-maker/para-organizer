# PARA Organizer

A self-hosted Second Brain system that auto-classifies notes using an LLM into **P**rojects / **A**reas / **R**esources / **A**rchives (PARA). Integrates with the Hermes Agent (via MCP), a Telegram Bot, and a Web UI.

## Features

- **Auto-classify notes with LLM** (`deepseek-v4-flash`) into Projects / Areas / Resources / Archives
- **Auto-deadline extraction** from note content
- **Auto-tagging** with confidence-based routing (low-confidence notes get flagged for review)
- **Full-text search** (SQLite FTS5) **+ semantic search** (sqlite-vec embeddings, hybrid RAG)
- **MCP server** with 15 tools for direct Hermes Agent integration (add/search/list/get/move/archive/stats/deadlines/digest/link/update/complete/delete/reclassify/ask)
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

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full deployment guide, including production setup on Contabo.

## Architecture

PARA Organizer is a single FastAPI application backed by SQLite (WAL mode, FTS5, sqlite-vec) with no external database dependency. Notes come in via the Web UI, Telegram bot, a Hermes cron webhook, or direct MCP tool calls. An LLM (Ollama Cloud) classifies and tags each note, extracts deadlines, and powers chat/RAG features. APScheduler runs background jobs (reclassification, archiving, digests, embeddings) inside the same process.

```
Web UI / Telegram / Hermes (MCP or cron webhook)
                │
                ▼
        FastAPI app (app/main.py)
   ┌─────────┬──────────┬─────────────┐
   │Classifier│Scheduler │ MCP server  │
   │  (LLM)   │(APScheduler)│(stdio)   │
   └─────────┴──────────┴─────────────┘
                │
                ▼
     SQLite (WAL + FTS5 + sqlite-vec)
```

## Tech Stack

| Layer | Technology |
|---|---|
| API server | FastAPI + Uvicorn |
| Database | SQLite (WAL mode, FTS5 full-text search, sqlite-vec for embeddings) |
| LLM | Ollama Cloud (`deepseek-v4-flash` primary, `gpt-oss:20b` fallback/chat) |
| Scheduler | APScheduler |
| Bot | python-telegram-bot |
| Agent integration | MCP (`mcp` Python SDK, stdio transport) |
| Templating | Jinja2 |
| Containerization | Docker + docker-compose |
| Testing | pytest + pytest-asyncio |

## Deployment

This project uses **GitHub Flow**: create a feature branch → open a PR → merge to `main` = deploy.

Merging a pull request into `main` triggers an automatic deployment to production (Contabo server).

**Deployment process:**

1. Create a feature branch from `main`
2. Implement changes
3. Open a Pull Request to `main`
4. Review and test
5. Merge the PR to `main` → auto-deploys to production

**Production URL:** https://para.mxlabs.cloud

For manual deployment steps and the full Docker guide, see [DEPLOYMENT.md](DEPLOYMENT.md).

## Project Structure

```
para-organizer/
├── app/
│   ├── main.py              # FastAPI app entrypoint
│   ├── config.py            # Settings (env-driven)
│   ├── database.py          # SQLite connection, schema, FTS5/sqlite-vec setup
│   ├── models.py            # PARA categories, data models
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
│   ├── mcp/mcp_server.py     # MCP server (15 tools) for Hermes integration
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
| `PARA_PORT` | Port the app listens on (default `8731`) |
| `PARA_DB_PATH` | SQLite database file path |
| `PARA_SECRET_KEY` | Bearer token for authenticated API endpoints |
| `OLLAMA_API_KEY` | Ollama Cloud API key (required for classification) |
| `LLM_PRIMARY` / `LLM_FALLBACK` | Classification models |
| `CHAT_MODEL` | Model used for conversational chat |
| `EMBED_PROVIDER` / `EMBED_MODEL` | Embedding provider/model for semantic search |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (optional) |
| `TELEGRAM_ALLOWED_USERS` | Comma-separated allowed Telegram user IDs |
| `AUTO_ARCHIVE_DAYS` | Days of inactivity before auto-archiving a note |
| `WEB_PUBLIC_URL` | Public base URL (used for webhooks, links) |

## API Overview

PARA Organizer exposes a **REST API** (FastAPI, see `app/routes/`) for the Web UI and general HTTP integrations, and an **MCP server** (`app/mcp/mcp_server.py`) exposing 15 tools for direct use by Hermes or other MCP-compatible agents. See [agent_skill.md](agent_skill.md) for the full integration guide covering both channels, authentication, and request/response formats.

## Contributing

1. Create a feature branch from `main`
2. Make your changes and add/update tests
3. Open a Pull Request to `main`
4. After review and passing tests, merge — this deploys automatically to production
