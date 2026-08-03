# PARA Organizer — Docker Deployment Guide

> **Date:** 2025-07-26 | **Target:** Production on Contabo / mxlabs.cloud

---

## 🚀 Deployment Strategy (GitHub Flow)

This project deploys via **GitHub Flow**: merging a Pull Request into `main` is the deployment trigger.

**Workflow:**

1. Create a feature branch from `main`
2. Implement and test your changes locally
3. Open a Pull Request targeting `main`
4. Review and test the PR
5. Merge the PR into `main` → **this automatically deploys to production**

There is no separate manual "release" step for normal changes — `main` is always the deployed state. Production runs at **https://para.mxlabs.cloud**, hosted on a Contabo VPS.

The rest of this document covers the underlying Docker deployment mechanics (used by the automated deploy and available for manual/local use).

---

## 📋 Prerequisites

- Docker Desktop หรือ Docker Engine >= 20.10
- Docker Compose >= 2.0
- Git (สำหรับ clone repository)
- Ollama Cloud API key (สำหรับ LLM classifier)
- Telegram Bot Token (optional, สำหรับ Telegram integration)

---

## 🚀 Quick Start (5 minutes)

### 1. Clone & Setup

```bash
cd ~/workspace
git clone https://github.com/lazymarcus005-maker/para-organizer.git
cd para-organizer

# Copy .env.example to .env
cp .env.example .env
```

### 2. Configure Environment

แก้ไข `.env` ใส่ค่า API keys:

```bash
# อย่างน้อยต้องมี 2 ตัวนี้
OLLAMA_API_KEY=sk-xxx-your-key-xxx
TELEGRAM_BOT_TOKEN=123456:ABCDEFxxxx (optional)

# ปรับ domain ถ้าใช้ production
WEB_PUBLIC_URL=https://para.mxlabs.cloud
```

### 3. Start Containers

```bash
# Build & start in background
docker-compose up -d

# Verify all services running
docker-compose ps

# Expected output:
# NAME                          STATUS
# para-organizer-init           Exited (0)
# para-organizer-app            Up (healthy)
```

### 4. Test

```bash
# Check API
curl http://localhost:8731/api/stats

# Open Web UI
open http://localhost:8731
```

**Done!** ✅

---

## 🐳 Docker Compose Architecture

```
┌─────────────────────────────────────────┐
│  docker-compose.yml (3 services)        │
├─────────────────────────────────────────┤
│                                         │
│  1. para-init (one-shot DB init)       │
│     └─ runs scripts/init_db.py          │
│     └─ mounts para-data volume          │
│                                         │
│  2. para-app (main FastAPI app)        │
│     └─ Python 3.12 + FastAPI           │
│     └─ port 8731                       │
│     └─ depends_on para-init             │
│     └─ healthcheck enabled              │
│                                         │
│  3. para-nginx (optional)               │
│     └─ Reverse proxy + SSL              │
│     └─ Rate limiting                    │
│     └─ Gzip compression                 │
│     └─ ports 80/443                     │
│                                         │
└─────────────────────────────────────────┘
         ↓ volume mount ↓
    para-data volume (/var/lib/para-organizer/data)
         ↓
    SQLite para.db
```

---

## 📁 Project Structure

```
para-organizer/
├── app/
│   ├── main.py              ← FastAPI app entry
│   ├── config.py            ← Settings
│   ├── database.py          ← SQLite
│   ├── classifier.py        ← LLM
│   ├── scheduler.py         ← APScheduler
│   ├── notifier.py          ← Telegram notifier
│   ├── routes/              ← API endpoints
│   ├── integrations/        ← Telegram bot
│   ├── mcp/                 ← Hermes MCP server
│   └── templates/           ← Jinja2 HTML
├── scripts/
│   ├── init_db.py           ← Initialize SQLite
│   └── seed.py              ← Seed test data
├── tests/                   ← Test suite
├── Dockerfile               ← Multi-stage build
├── docker-compose.yml       ← Container orchestration
├── nginx.conf               ← Reverse proxy config
├── requirements.txt         ← Python dependencies
├── .env.example             ← Environment template
└── spec.md                  ← Full technical spec
```

---

## 🛠️ Common Commands

### Container Management

```bash
# Start containers
docker-compose up -d

# Stop containers
docker-compose down

# View logs
docker-compose logs -f para-app

# Rebuild image (after code changes)
docker-compose up -d --build

# Remove all data (be careful!)
docker-compose down -v
```

### Database Operations

```bash
# Initialize database
docker-compose exec para-app python3 scripts/init_db.py

# Seed test data
docker-compose exec para-app python3 scripts/seed.py

# Connect to SQLite shell
docker-compose exec para-app sqlite3 /var/lib/para-organizer/data/para.db

# Backup database
docker-compose exec para-app cp \
  /var/lib/para-organizer/data/para.db \
  /var/lib/para-organizer/data/para.db.backup
```

### API Testing

```bash
# Health check
curl http://localhost:8731/api/stats

# Create a note
curl -X POST http://localhost:8731/api/notes \
  -H "Content-Type: application/json" \
  -d '{
    "title": "ต่อทะเบียนรถ",
    "content": "ทะเบียนหมดอายุ 15 สิงหาคม 2025",
    "source": "manual"
  }'

# Search notes
curl "http://localhost:8731/api/search?q=ทะเบียน"

# Get PARA tree
curl http://localhost:8731/api/para/tree

# Export as JSON
curl http://localhost:8731/api/export?format=json > notes.json
```

### Telegram Bot Testing

```bash
# Send test message to webhook
curl -X POST http://localhost:8731/webhook/telegram \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "text": "/note ทดสอบ note ใหม่",
      "chat": {"id": 123456789}
    }
  }'
```

---

## 🔒 Production Deployment on Contabo

### Step 1: SSH to Contabo Server

```bash
ssh root@your-server-ip
cd /opt/para-organizer
```

### Step 2: Clone Repository

```bash
git clone https://github.com/lazymarcus005-maker/para-organizer.git
cd para-organizer
```

### Step 3: Setup Environment

```bash
# Create .env from template
cp .env.example .env

# Edit with your production keys
nano .env

# Required settings:
# PARA_SECRET_KEY=<random-secret-key> (generate: openssl rand -hex 32)
# OLLAMA_API_KEY=<your-key>
# TELEGRAM_BOT_TOKEN=<your-token>
# WEB_PUBLIC_URL=https://para.mxlabs.cloud
# TELEGRAM_WEBHOOK_URL=https://para.mxlabs.cloud/webhook/telegram
```

### Step 4: Prepare Directories & Permissions

```bash
# Create data directory on host
mkdir -p /var/lib/para-organizer/data
chmod 755 /var/lib/para-organizer/data

# Optionally mount from different disk
# mount /dev/vdX /var/lib/para-organizer/data
```

### Step 5: Start with Docker Compose

```bash
docker-compose up -d

# Monitor startup
docker-compose logs -f para-app

# Wait for healthy status (30 seconds)
docker-compose ps

# Verify API
curl http://localhost:8731/api/stats
```

### Step 6: Enable Nginx Reverse Proxy (Optional)

```bash
# Uncomment para-nginx in docker-compose.yml
nano docker-compose.yml
# Uncomment the "para-nginx" service block

# Generate self-signed cert (for testing)
mkdir -p certs
openssl req -x509 -newkey rsa:4096 -nodes \
  -out certs/cert.pem -keyout certs/key.pem -days 365

# Or use Let's Encrypt (recommended for production)
# with Certbot instead

# Restart with Nginx
docker-compose up -d --build
```

### Step 7: Setup SSL with Cloudflare (Recommended)

Since you're using Cloudflare for DNS on mxlabs.cloud:

```bash
# In Cloudflare dashboard:
# 1. Go to SSL/TLS > Origin Server
# 2. Create a certificate (valid 15 years)
# 3. Save cert.pem + key.pem to ./certs/

# Update docker-compose to mount certs
# volumes:
#   - ./certs:/etc/nginx/certs:ro

# Restart
docker-compose restart para-nginx
```

### Step 8: Systemd Service (Auto-start on Reboot)

Create `/etc/systemd/system/para-organizer.service`:

```ini
[Unit]
Description=PARA Organizer Docker Compose
Requires=docker.service
After=docker.service

[Service]
Type=simple
WorkingDirectory=/opt/para-organizer
ExecStart=/usr/bin/docker-compose up
ExecStop=/usr/bin/docker-compose down
Restart=unless-stopped
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable para-organizer
sudo systemctl start para-organizer
sudo systemctl status para-organizer
```

---

## 📊 Monitoring & Logging

### View Logs

```bash
# Real-time app logs
docker-compose logs -f para-app

# Nginx logs (if enabled)
docker-compose logs -f para-nginx

# Last 100 lines
docker-compose logs --tail 100

# Specific time range
docker-compose logs --since 1h --until 10m
```

### Health Check

```bash
# Check container status
docker-compose ps

# Container stats (CPU, memory)
docker stats para-organizer-app

# Detailed inspect
docker inspect para-organizer-app
```

### Backup & Restore

```bash
# Backup database
docker-compose exec para-app bash -c \
  'sqlite3 /var/lib/para-organizer/data/para.db ".dump"' > backup.sql

# Restore from backup
cat backup.sql | docker-compose exec -T para-app \
  sqlite3 /var/lib/para-organizer/data/para.db

# Backup entire data directory
docker-compose exec para-app tar czf - \
  /var/lib/para-organizer/data | gzip > data-backup.tar.gz

# Restore
tar xzf data-backup.tar.gz -C /var/lib/para-organizer/
```

---

## 🔧 Troubleshooting

### Container won't start

```bash
# Check logs
docker-compose logs para-app

# Common issues:
# 1. Database locked: restart para-app
docker-compose restart para-app

# 2. Port already in use: change port in docker-compose.yml
# ports:
#   - "8732:8731"

# 3. Env var missing: verify .env exists and is valid
cat .env
```

### LLM Classifier Failing

```bash
# Check OLLAMA_API_KEY
docker-compose exec para-app env | grep OLLAMA

# Test LLM connection
docker-compose exec para-app python3 -c "
import httpx
import os
api_key = os.getenv('OLLAMA_API_KEY')
result = httpx.post(
  'https://ollama.com/v1/chat/completions',
  json={'model': 'deepseek-v4-flash', 'messages': [{'role': 'user', 'content': 'test'}]},
  headers={'Authorization': f'Bearer {api_key}'}
).json()
print(result)
"
```

### Telegram Webhook Issues

```bash
# Verify webhook URL is correct
docker-compose exec para-app env | grep TELEGRAM

# Test webhook manually
curl -X POST http://localhost:8731/webhook/telegram \
  -H "Content-Type: application/json" \
  -d '{"message": {"text": "/help", "chat": {"id": 123}}}'

# Check Telegram bot token
curl https://api.telegram.org/bot<TOKEN>/getMe
```

### Disk Space Issues

```bash
# Check volume size
docker inspect para-data

# Check used space
du -sh /var/lib/para-organizer/data

# Cleanup Docker
docker system prune -a --volumes
```

---

## 📈 Performance Tuning

### Environment Variables for Production

```bash
# .env adjustments
PARA_PORT=8731

# LLM optimization (reduce timeouts if network is fast)
LLM_TIMEOUT=30
LLM_MAX_RETRIES=1

# Scheduler optimization
RECLASSIFY_INTERVAL_HOURS=12   # Less frequent
AUTO_ARCHIVE_DAYS=60            # Archive older

# Notification tuning
NOTIFY_DEADLINE_DAYS=7          # Only 7 days before
NOTIFY_STALE_DAYS=30           # Less frequent stale checks

# Telegram
TELEGRAM_BOT_TOKEN=<your-token>
```

### Docker Resource Limits

Uncomment in `docker-compose.yml`:

```yaml
services:
  para-app:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 1G
        reservations:
          cpus: '1'
          memory: 512M
```

### SQLite Performance

```bash
# Enable WAL mode (already in app/database.py)
# But you can manually verify:
docker-compose exec para-app sqlite3 /var/lib/para-organizer/data/para.db "PRAGMA journal_mode;"
# Should return: wal

# Check indexes
docker-compose exec para-app sqlite3 /var/lib/para-organizer/data/para.db ".indices"
```

---

## 🔐 Security Hardening

### 1. Change Secret Keys

```bash
# Generate random secret
openssl rand -hex 32
# Output: a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6

# Update .env
PARA_SECRET_KEY=a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6
```

### 2. Restrict Telegram Users

```bash
# Only specific Telegram user IDs
TELEGRAM_ALLOWED_USERS=123456789,987654321
```

### 3. Use HTTPS

Use Cloudflare SSL + Nginx reverse proxy

### 4. Database Backups

```bash
# Automatic daily backups via cron
0 2 * * * cd /opt/para-organizer && \
  docker-compose exec -T para-app bash -c \
  'cp /var/lib/para-organizer/data/para.db /var/lib/para-organizer/data/para.db.$(date +%Y%m%d)'
```

### 5. Firewall Rules

```bash
# Allow only necessary ports
sudo ufw allow 22/tcp      # SSH
sudo ufw allow 80/tcp      # HTTP (for ACME)
sudo ufw allow 443/tcp     # HTTPS
sudo ufw enable
```

---

## 📦 Updating the Application

```bash
# 1. Backup database first
docker-compose exec para-app cp \
  /var/lib/para-organizer/data/para.db \
  /var/lib/para-organizer/data/para.db.backup

# 2. Pull latest code
git pull origin main

# 3. Rebuild and restart
docker-compose up -d --build

# 4. Run migrations (if any)
docker-compose exec para-app python3 scripts/init_db.py

# 5. Verify
docker-compose logs -f para-app
```

---

## 🎯 Next Steps

1. ✅ Deploy containers
2. ✅ Test API endpoints
3. ✅ Setup Telegram bot (if needed)
4. ✅ Configure Hermes MCP server
5. ✅ Setup cron jobs for Hermes integration
6. 📊 Monitor performance & logs
7. 🔄 Setup automated backups

---

## 📞 Support

- **Logs:** `docker-compose logs -f para-app`
- **Database:** `/var/lib/para-organizer/data/para.db`
- **Config:** `.env`
- **API Docs:** http://localhost:8731/docs (FastAPI auto-generated)
- **Spec:** `spec.md` in repo

---

**Happy deploying!** 🚀
