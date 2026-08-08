# AGENTS.md — PARA Organizer

Project context for AI coding agents (Claude Code, Codex, OpenCode, etc.) working in this repository.

## Project Overview

PARA Organizer is a self-hosted Second Brain system. Notes come in from the Web UI, Telegram, or a Hermes Agent (via MCP tools or a cron webhook), and an LLM auto-classifies them into **P**rojects / **A**reas / **R**esources / **A**rchives, extracts deadlines, and applies tags. It supports full-text (PostgreSQL `tsvector`) and semantic (`pgvector`) search, scheduled automation jobs, note linking/distillation, and a weekly AI review. See [README.md](README.md) for the full feature list and [SECOND_BRAIN_ROADMAP.md](SECOND_BRAIN_ROADMAP.md) for design history.

Production is the `main` branch: PostgreSQL v5, deployed on Dokploy (self-hosted PaaS). `main` is the production source of truth. The older SQLite v4 version is retired from production and preserved only on the `backup/sqlite-version` branch for historical reference — see "Legacy vs. production files" below.

## Architecture

- **`app/main.py`** — FastAPI app entrypoint; wires up routes and startup (DB init, cache init)
- **`app/database_v2.py`** / **`app/models_v2.py`** — **Production (v5, PostgreSQL).** Async SQLAlchemy engine/session (`async_session_factory`) and ORM models for PostgreSQL 16 + pgvector, reached via `PARA_DB_URL` (external host `169.58.65.88:5436/paradb`). Full-text search via `tsvector`, embeddings via `pgvector`. This is the current schema — treat changes as high-impact.
- **`app/database.py`** / **`app/models.py`** — **Legacy (v4, SQLite).** aiosqlite engine (WAL mode, FTS5, sqlite-vec), local `para.db`. These files still physically exist in this tree but are **not** the production code path — don't edit or rely on them thinking they're current. The full v4 system is preserved only on the `backup/sqlite-version` branch.
- **`app/classifier.py`** — LLM-based classification, tagging, deadline extraction (Ollama Cloud, primary `deepseek-v4-flash`, fallback `gpt-oss:20b`; embeddings via `nomic-embed-text`)
- **`app/chat.py`** — conversational / hybrid RAG chat mode
- **`app/scheduler.py`** — Legacy in-process APScheduler (v4). Replaced by `app/scheduler_service.py` in production.
- **`app/scheduler_service.py`** — Production standalone APScheduler service (`para-scheduler`) with Redis singleton lock. Pushes jobs to Redis task queue.
- **`app/task_queue.py`** — Redis-backed async task queue (8 topics: classify, embed, notify, link, distill, review, backup, escalate)
- **`app/worker.py`** — Production background worker process (`para-worker`) consuming from all Redis queue topics
- **`app/cache.py`** — Redis cache layer with get/set/invalidate, used by read-heavy endpoints
- **`app/mcp/mcp_server.py`** — Legacy MCP server (stdio transport). Local-dev only — not used in production.
- **`app/mcp/mcp_server_http.py`** — **Production MCP server** (`para-mcp`), HTTP SSE transport, port 8100, 27 tools, connection pooling. SSE endpoint: `https://mc-para.mxlabs.cloud/mcp/sse`.
- **`app/routes/`** — FastAPI routers for the REST API and Web UI pages
- **`app/routes/health.py`** — Health check endpoints (`/api/health`, `/api/health/ready`, `/api/health/live`)
- **`app/integrations/`** — Telegram bot (chat, voice STT, inline buttons)
- **`app/embed.py`** / **`app/vector_store.py`** — embedding generation and semantic search
- **`app/linker.py`**, **`app/distill.py`**, **`app/review.py`**, **`app/planner.py`**, **`app/graph.py`** — note linking, distillation, weekly review, action planning, relationship graph

In the legacy v4 architecture, everything ran in one FastAPI process. In production (v5), the app is decomposed into 6 Docker services defined in `docker-compose.yml`, all `replicas: 1` (single/two-user deployment): `para-app` (FastAPI, port 8731, host `para.mxlabs.cloud`), `para-mcp` (MCP HTTP SSE, port 8100, host `mc-para.mxlabs.cloud`), `para-worker` (background task consumer), `para-scheduler` (APScheduler singleton), `para-backup` (optional cloud backup), and `para-redis` (Redis 7, task queue + cache) — see README.md Architecture. See [agent_skill.md](agent_skill.md) for the detailed REST API + MCP integration reference (endpoints, auth, request/response shapes).

### Legacy vs. production files

- **Production code path**: `app/database_v2.py`, `app/models_v2.py`, `app/mcp/mcp_server_http.py`, plus everything else under `app/` except the two files below.
- **Legacy (v4 SQLite) code path**: `app/database.py`, `app/models.py`, `app/scheduler.py`, `app/mcp/mcp_server.py` (stdio). These are kept for local-dev/reference but are not deployed. The complete SQLite v4 system is preserved on the `backup/sqlite-version` branch for historical reference only.

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

# Run background worker (v5 distributed mode)
python3 -m app.worker

# Run standalone scheduler (v5 distributed mode)
python3 -m app.scheduler_service

# Run MCP HTTP SSE server (v5 distributed mode, port 8100)
python3 -m app.mcp.mcp_server_http
```

## Code Standards

- **Python 3.12+**, fully type-hinted function signatures (`str | None`, not `Optional[str]`)
- **Async-first**: route handlers, DB access, and LLM calls are `async def`; use the async SQLAlchemy sessions from `app/database_v2.py` (`async_session_factory`) against PostgreSQL, don't mix in blocking I/O on the request path
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

`app/database_v2.py` and `app/models_v2.py` (the production PostgreSQL schema) are treated as frozen/shared — changes there affect every lane, so review them carefully regardless of which agent is editing. The legacy `app/database.py` / `app/models.py` (v4 SQLite) are no longer the live shared schema — see "Legacy vs. production files" above. When working across lanes in a single change, be extra careful about merge conflicts and prefer smaller, focused PRs (see README's GitHub Flow section).

## Deployment Status

The PostgreSQL + Redis + worker stack (v5) has already been merged into `main` and is the live production system — `main` is production-live, deployed on Dokploy (self-hosted PaaS). Dokploy watches `main` and automatically redeploys on merge (GitHub Flow: feature branch → PR → merge to `main`). The old SQLite v4 architecture is retired from production and preserved only on the `backup/sqlite-version` branch for historical reference.

## Important Conventions

- **Thai-first**: UI copy, chat responses, and classification prompts support Thai language natively — don't assume English-only input/output when touching classifier, chat, or template code
- **PostgreSQL in production**: PostgreSQL 16 + pgvector is the live database (`tsvector` for full-text, `pgvector` for embeddings) — schema changes go through `app/database_v2.py` and `app/models_v2.py`. Don't confuse this with the legacy SQLite files (`app/database.py`, `app/models.py`), which are not in the production path
- **No npm/build step**: the Web UI is server-rendered Jinja2 + vanilla JS/CSS served directly from `app/static/`; don't introduce a JS bundler or frontend framework
- **GitHub Flow deployment**: merging a PR to `main` is picked up by Dokploy, which automatically redeploys production (https://para.mxlabs.cloud) — treat `main` as production-live, keep changes tested before merging

## Reference

For the detailed MCP tool reference and REST API integration guide (auth, endpoints, request/response formats) used by external agents like Hermes, see **[agent_skill.md](agent_skill.md)**.
