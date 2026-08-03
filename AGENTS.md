# AGENTS.md — PARA Organizer

Project context for AI coding agents (Claude Code, Codex, OpenCode, etc.) working in this repository.

## Project Overview

PARA Organizer is a self-hosted Second Brain system. Notes come in from the Web UI, Telegram, or a Hermes Agent (via MCP tools or a cron webhook), and an LLM auto-classifies them into **P**rojects / **A**reas / **R**esources / **A**rchives, extracts deadlines, and applies tags. It supports full-text (FTS5) and semantic (sqlite-vec) search, scheduled automation jobs, note linking/distillation, and a weekly AI review. See [README.md](README.md) for the full feature list and [SECOND_BRAIN_ROADMAP.md](SECOND_BRAIN_ROADMAP.md) for design history.

## Architecture

- **`app/main.py`** — FastAPI app entrypoint; wires up routes and startup (DB init, scheduler)
- **`app/database.py`** / **`app/models.py`** — SQLite schema (WAL mode, FTS5, sqlite-vec), PARA category definitions. Shared foundation — treat schema changes as high-impact.
- **`app/classifier.py`** — LLM-based classification, tagging, deadline extraction (Ollama Cloud, `deepseek-v4-flash`)
- **`app/chat.py`** — conversational / hybrid RAG chat mode
- **`app/scheduler.py`** — APScheduler jobs (reclassify, auto-archive, escalation, deadline reminders, stale detection, digests, weekly review, embedding backfill)
- **`app/mcp/mcp_server.py`** — MCP server (stdio transport), 15 tools exposed to Hermes (`para_add_note`, `para_search`, `para_list`, `para_get`, `para_move`, `para_archive`, `para_stats`, `para_deadlines`, `para_digest`, `para_add_link`, `para_update`, and more)
- **`app/routes/`** — FastAPI routers for the REST API and Web UI pages
- **`app/integrations/`** — Telegram bot (chat, voice STT, inline buttons)
- **`app/embed.py`** / **`app/vector_store.py`** — embedding generation and semantic search
- **`app/linker.py`**, **`app/distill.py`**, **`app/review.py`**, **`app/planner.py`**, **`app/graph.py`** — note linking, distillation, weekly review, action planning, relationship graph

Everything runs in one FastAPI process — no separate workers, no message queue, no external database. See [agent_skill.md](agent_skill.md) for the detailed REST API + MCP integration reference (endpoints, auth, request/response shapes).

## Key Commands

```bash
# Run the app locally (requires .env, see README Quick Start)
uvicorn app.main:app --reload --port 8731

# Run via Docker (preferred for parity with production)
docker-compose up -d

# Run the test suite
pytest

# Run a single test file
pytest tests/test_classifier.py -v

# Initialize / migrate the database
python3 scripts/init_db.py
```

## Code Standards

- **Python 3.12+**, fully type-hinted function signatures (`str | None`, not `Optional[str]`)
- **Async-first**: route handlers, DB access, and LLM calls are `async def`; use `aiosqlite`/async connections, don't mix in blocking I/O on the request path
- MCP tools return JSON-serializable dicts/lists; expected errors (not found, invalid input) are returned as `{"error": ...}` dicts rather than raised, so the MCP server never crashes on a bad call
- Keep new dependencies minimal — this project intentionally avoids a build step or JS package manager (see Conventions below)
- Follow existing patterns in the module you're editing rather than introducing new abstractions

## File Ownership (Lane System)

Historically, work was split across three agent lanes to allow safe parallel development. This ownership pattern is documented in detail in [SECOND_BRAIN_ROADMAP.md](SECOND_BRAIN_ROADMAP.md); the gist:

| Lane | Focus | Example files |
|---|---|---|
| Heavy backend / AI / MCP | RAG, MCP protocol, LLM prompt engineering | `app/mcp/mcp_server.py`, `app/classifier.py`, `app/chat.py`, `app/linker.py`, `app/review.py`, `app/usage.py`, `app/vector_store.py` |
| Automation / Scheduler / Telegram | Scheduler jobs, Telegram handlers, notifications | `app/scheduler.py`, `app/notifier.py`, `app/embed.py`, `app/integrations/telegram_bot.py`, `app/routes/telegram_webhook.py`, `app/routes/cron_webhook.py` |
| UI / Templates / Docs / Ops | Frontend, docs, backup/ops scripts | `app/templates/`, `app/static/`, `app/routes/backup.py`, `app/routes/pages.py`, `*.md` |

`app/database.py` and `app/models.py` (schema) are treated as frozen/shared — changes there affect every lane, so review them carefully regardless of which agent is editing. When working across lanes in a single change, be extra careful about merge conflicts and prefer smaller, focused PRs (see README's GitHub Flow section).

## Important Conventions

- **Thai-first**: UI copy, chat responses, and classification prompts support Thai language natively — don't assume English-only input/output when touching classifier, chat, or template code
- **SQLite-only**: no Postgres/MySQL, no ORM migrations framework — schema changes go through `app/database.py` and `scripts/init_db.py` directly
- **No npm/build step**: the Web UI is server-rendered Jinja2 + vanilla JS/CSS served directly from `app/static/`; don't introduce a JS bundler or frontend framework
- **GitHub Flow deployment**: merging a PR to `main` auto-deploys to production (https://para.mxlabs.cloud) — treat `main` as production-live, keep changes tested before merging

## Reference

For the detailed MCP tool reference and REST API integration guide (auth, endpoints, request/response formats) used by external agents like Hermes, see **[agent_skill.md](agent_skill.md)**.
