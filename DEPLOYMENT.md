# PARA Organizer — Production Deployment Guide

> **Target:** Production on Dokploy (self-hosted PaaS) at `para.mxlabs.cloud` / `mc-para.mxlabs.cloud`

---

## 🚀 Deployment Strategy (GitHub Flow + Dokploy)

This project deploys via **GitHub Flow**: merging a Pull Request into `main` is the deployment trigger.

**Workflow:**

1. Create a feature branch from `main`
2. Implement and test your changes locally
3. Open a Pull Request targeting `main`
4. Review and test the PR
5. Merge the PR into `main` → **Dokploy pulls `main` and redeploys automatically**

There is no separate manual "release" step for normal changes — `main` is always the deployed state. `main` is the production source of truth: it contains the PostgreSQL v5 codebase. The old SQLite v4 codebase is preserved for historical reference on the `backup/sqlite-version` branch only — it is not deployed anywhere.

Production runs at:
- **REST API:** https://para.mxlabs.cloud
- **MCP HTTP SSE:** https://mc-para.mxlabs.cloud/mcp/sse

Dokploy manages the reverse proxy (built-in Traefik with Let's Encrypt auto-cert), container orchestration, and redeploys from this repo's `docker-compose.yml`.

---

## 📋 Prerequisites

- A Dokploy instance with access to a Docker Swarm-capable host and the `dokploy-network` overlay network
- An externally managed PostgreSQL 16 + pgvector database (production uses `169.58.65.88:5436/paradb`) — this is **not** provisioned by `docker-compose.yml`
- Ollama Cloud API key (for the LLM classifier and chat)
- Telegram Bot Token (optional, for Telegram integration)
- Git access to this repository

---

## 🐳 Docker Compose Architecture

`docker-compose.yml` defines 6 services, all deployed with `replicas: 1` (single/two-user deployment — see `x-healthcheck` and each service's `deploy.replicas`):

```text
1. para-redis      — Redis 7, task queue + cache
2. para-app        — FastAPI REST API, port 8731, Traefik host para.mxlabs.cloud
3. para-worker     — background task consumer (python3 -m app.worker)
4. para-scheduler  — APScheduler singleton (python3 -m app.scheduler_service)
5. para-mcp        — MCP HTTP SSE server, port 8100, Traefik host mc-para.mxlabs.cloud
6. para-backup     — optional cloud backup (python3 -m app.scheduler_service --backup-only)
```

All services except `para-redis` connect to the external PostgreSQL database via `PARA_DB_URL`. `para-app`, `para-mcp`, and `para-worker`/`para-scheduler` join both the internal `para-overlay` network and Dokploy's `dokploy-network` (for Traefik routing).

---

## 📁 Project Structure

```
para-organizer/
├── app/
│   ├── main.py               ← FastAPI app entry
│   ├── config.py              ← Settings (env-driven)
│   ├── database_v2.py         ← PostgreSQL async engine/session (production)
│   ├── models_v2.py           ← PostgreSQL SQLAlchemy models (production)
│   ├── database.py            ← SQLite (legacy v4, see backup/sqlite-version)
│   ├── classifier.py          ← LLM classification
│   ├── worker.py              ← Redis task queue consumer
│   ├── scheduler_service.py   ← Standalone APScheduler process
│   ├── notifier.py            ← Telegram notifier
│   ├── routes/                ← API endpoints
│   ├── integrations/          ← Telegram bot
│   ├── mcp/
│   │   ├── mcp_server_http.py ← MCP HTTP SSE server (production, 27 tools)
│   │   └── mcp_server.py      ← MCP stdio server (local-dev only)
│   └── templates/             ← Jinja2 HTML
├── alembic/                   ← PostgreSQL migrations
├── scripts/                   ← DB init / maintenance scripts
├── tests/                     ← Test suite
├── Dockerfile                 ← Multi-stage build (shared by all services)
├── docker-compose.yml         ← 6-service orchestration (Dokploy-managed)
├── requirements.txt           ← Python dependencies
├── .env.example                ← Environment template
└── spec.md                    ← Full technical spec
```

---

## 🔧 Environment Variables

Set these on the Dokploy project (or in `.env` for local docker-compose use). See `.env.example` for the complete list.

### Core

| Variable | Purpose |
|---|---|
| `PARA_DB_URL` | PostgreSQL connection string, e.g. `postgresql+asyncpg://paradb:<password>@169.58.65.88:5436/paradb` |
| `PARA_REDIS_URL` | Redis connection string, e.g. `redis://para-redis:6379/0` |
| `PARA_DB_POOL_SIZE` / `PARA_DB_MAX_OVERFLOW` | SQLAlchemy async pool sizing (defaults `10` / `20`) |
| `PARA_REDIS_CACHE_TTL` | Read-cache TTL in seconds (default `60`) |
| `PARA_SECRET_KEY` | Bearer token for authenticated API endpoints — generate with `openssl rand -hex 32` |
| `PARA_PORT` | Port `para-app` listens on (default `8731`) |

### LLM (Ollama Cloud)

| Variable | Purpose |
|---|---|
| `OLLAMA_API_KEY` | Required for classification/chat/embeddings |
| `OLLAMA_BASE_URL` | Default `https://ollama.com/v1` |
| `LLM_PRIMARY` / `LLM_FALLBACK` | Default `deepseek-v4-flash` / `gpt-oss:20b` |
| `EMBED_MODEL` | Default `nomic-embed-text` |

### Telegram / Notifications / Web

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_WEBHOOK_URL` / `TELEGRAM_ALLOWED_USERS` | Telegram integration (optional) |
| `NOTIFY_CHANNEL`, `NOTIFY_DEADLINE_DAYS`, `NOTIFY_DIGEST_DAY`, `NOTIFY_DIGEST_TIME`, `NOTIFY_STALE_DAYS` | Notification scheduling |
| `AUTO_ARCHIVE_DAYS`, `RECLASSIFY_INTERVAL_HOURS`, `RECLASSIFY_CONFIDENCE_THRESHOLD` | Scheduler job tuning |
| `WEB_PUBLIC_URL` | `https://para.mxlabs.cloud` in production |

### Backup (optional, `para-backup` service)

| Variable | Purpose |
|---|---|
| `BACKUP_CLOUD_ENABLED` | `true`/`false` |
| `BACKUP_CLOUD_ENDPOINT`, `BACKUP_CLOUD_BUCKET`, `BACKUP_CLOUD_ACCESS_KEY`, `BACKUP_CLOUD_SECRET_KEY` | S3-compatible storage credentials |
| `BACKUP_CLOUD_RETENTION_DAYS` | Default `30` |

`PARA_DB_PATH` (SQLite file path) is a **legacy v4 variable** — it has no effect on the production PostgreSQL path and only matters for the code on `backup/sqlite-version`.

---

## 🚀 Deploying on Dokploy

1. **Connect the repo:** In Dokploy, point the project at this repository, branch `main`, using `docker-compose.yml` as the compose file (Dokploy auto-detects the 6 services).
2. **Set environment variables:** Configure the variables above in the Dokploy project's environment settings (they're injected into each service per the `environment:` blocks in `docker-compose.yml`).
3. **Verify the external database:** Confirm the PostgreSQL 16 + pgvector host referenced by `PARA_DB_URL` is reachable from the Dokploy host and that the `paradb` database + pgvector extension exist. Run Alembic migrations (`alembic upgrade head`) if this is a fresh database.
4. **Deploy:** Trigger a deploy in Dokploy (or push to `main` — Dokploy redeploys automatically on merge).
5. **Domains:** Dokploy's built-in Traefik reads the `traefik.*` labels in `docker-compose.yml` and provisions Let's Encrypt certificates for:
   - `para-app` → `para.mxlabs.cloud`
   - `para-mcp` → `mc-para.mxlabs.cloud`

### Local / manual docker-compose use

```bash
cp .env.example .env
# fill in OLLAMA_API_KEY, PARA_DB_URL (point at a reachable Postgres+pgvector instance), etc.
docker-compose up -d
docker-compose ps
```

---

## ✅ Verification

```bash
# Liveness / readiness (para-app)
curl https://para.mxlabs.cloud/api/health/live
curl https://para.mxlabs.cloud/api/health/ready

# REST API smoke test
curl https://para.mxlabs.cloud/api/stats

# MCP HTTP SSE — confirm the endpoint is up
curl -i https://mc-para.mxlabs.cloud/mcp/sse

# MCP container healthcheck (internal)
docker-compose exec para-mcp curl -f http://localhost:8100/health
```

`app/routes/health.py` reports PostgreSQL and Redis connectivity — check it if either dependency is misbehaving.

---

## 🛠️ Common Commands

```bash
# View logs for a service
docker-compose logs -f para-app
docker-compose logs -f para-mcp
docker-compose logs -f para-worker
docker-compose logs -f para-scheduler

# Rebuild after code changes (all services share one Dockerfile/image)
docker-compose up -d --build

# Restart a single service
docker-compose restart para-app

# Run Alembic migrations against the PostgreSQL database
docker-compose exec para-app alembic upgrade head
```

---

## 🔧 Troubleshooting

### Service won't start

```bash
docker-compose logs <service-name>
```

Common causes: `PARA_DB_URL` unreachable (check the external Postgres host/firewall), missing `OLLAMA_API_KEY`, or a port conflict on 8731/8100.

### LLM classifier failing

```bash
docker-compose exec para-app env | grep OLLAMA
docker-compose exec para-app python3 -c "
import httpx, os
api_key = os.getenv('OLLAMA_API_KEY')
result = httpx.post(
  'https://ollama.com/v1/chat/completions',
  json={'model': 'deepseek-v4-flash', 'messages': [{'role': 'user', 'content': 'test'}]},
  headers={'Authorization': f'Bearer {api_key}'}
).json()
print(result)
"
```

### Database connectivity

```bash
docker-compose exec para-app env | grep PARA_DB_URL
docker-compose exec para-app python3 -c "
import asyncio
from app.database_v2 import async_session_factory
from sqlalchemy import text
async def check():
    async with async_session_factory() as s:
        print(await s.execute(text('SELECT 1')))
asyncio.run(check())
"
```

### MCP SSE not reachable

- Confirm the `para-mcp` Traefik labels/host in `docker-compose.yml` match the DNS record for `mc-para.mxlabs.cloud`.
- Check `docker-compose logs -f para-mcp` and the container healthcheck (`GET /health` on port 8100).

### Telegram webhook issues

```bash
docker-compose exec para-app env | grep TELEGRAM
curl https://api.telegram.org/bot<TOKEN>/getMe
```

---

## 🔐 Security Hardening

1. **Secret key:** `PARA_SECRET_KEY` — generate with `openssl rand -hex 32`, set via Dokploy environment settings (never commit to `.env` in git).
2. **Restrict Telegram users:** set `TELEGRAM_ALLOWED_USERS` to a comma-separated allowlist.
3. **TLS:** handled by Dokploy's Traefik + Let's Encrypt — no manual cert management needed.
4. **Database credentials:** rotate the PostgreSQL password embedded in `PARA_DB_URL` periodically; it's shared across `para-app`, `para-mcp`, `para-worker`, `para-scheduler`, `para-backup`.
5. **Rate limiting:** `para-app`'s Traefik router has a rate-limit middleware (`para-ratelimit`, average 100 / burst 200) configured in `docker-compose.yml`.

---

## 📦 Updating the Application

1. Merge a PR to `main` — Dokploy redeploys automatically.
2. For schema changes, run `alembic upgrade head` against the PostgreSQL database (via `docker-compose exec para-app alembic upgrade head` or Dokploy's shell).
3. Watch `docker-compose logs -f para-app para-mcp para-worker para-scheduler` during rollout.
4. Re-run the [Verification](#-verification) checks above.

---

## 📞 Support

- **Logs:** `docker-compose logs -f <service>` (see service names above)
- **Database:** external PostgreSQL 16 + pgvector at the host in `PARA_DB_URL`
- **Config:** Dokploy environment settings (or `.env` for local use)
- **API Docs:** `https://para.mxlabs.cloud/docs` (FastAPI auto-generated)
- **Spec:** `spec.md` in repo
- **Legacy SQLite deployment notes:** see `backup/sqlite-version` branch (not applicable to production)
