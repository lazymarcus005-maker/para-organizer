# PARA Organizer — Quick Deploy Guide

## 🚀 5-Minute Startup

```bash
cd ~/workspace/PARA-organizer

# 1. Setup (only first time)
cp .env.example .env
# → Edit .env, add your OLLAMA_API_KEY & TELEGRAM_BOT_TOKEN

# 2. Start
bash deploy.sh up

# 3. Test
bash deploy.sh test

# 4. Access
# Web UI: http://localhost:8731
# API Docs: http://localhost:8731/docs
```

---

## 📋 Deployment Scripts (Quick Access)

| Command | What it does |
|---------|------------|
| `bash deploy.sh up` | 🟢 Start all containers |
| `bash deploy.sh down` | 🔴 Stop containers |
| `bash deploy.sh logs` | 📊 Show live logs |
| `bash deploy.sh status` | 📈 Health check |
| `bash deploy.sh test` | ✅ Test API endpoints |
| `bash deploy.sh backup` | 💾 Backup database |
| `bash deploy.sh shell` | 🐚 SSH into container |
| `bash deploy.sh seed` | 🌱 Add test data |
| `bash deploy.sh restart` | 🔄 Restart services |
| `bash deploy.sh clean` | 🗑️ Remove all data (dangerous!) |

---

## 🔧 Files Created

```
Dockerfile                  ← Multi-stage Python 3.12 image
docker-compose.yml          ← Container orchestration config
nginx.conf                  ← Reverse proxy + SSL + rate limiting
.dockerignore               ← Build optimization
.env.example                ← Environment template
.env.production             ← Production env template
deploy.sh                   ← Bash deployment script
DEPLOYMENT.md               ← Full deployment guide (13KB)
```

---

## 📍 Architecture

```
┌─────────────────────┐
│  Docker Containers  │
├─────────────────────┤
│  para-organizer-app │ ← FastAPI (port 8731)
│  para-organizer-init│ ← DB setup (one-shot)
│  para-organizer-....*  ← Nginx optional
└─────────────────────┘
         ↓ volume
    para-data (SQLite)
```

---

## 🌍 Deployment to Production (Contabo)

```bash
# SSH to server
ssh root@your-server-ip
cd /opt/para-organizer

# Clone & setup
git clone https://github.com/lazymarcus005-maker/para-organizer.git
cd para-organizer
cp .env.production .env

# Edit with your keys
nano .env
# OLLAMA_API_KEY=sk-...
# TELEGRAM_BOT_TOKEN=123:ABC...
# PARA_SECRET_KEY=<random>

# Start
bash deploy.sh up

# Enable HTTPS (optional but recommended)
# See DEPLOYMENT.md section "Setup SSL with Cloudflare"

# Auto-restart on reboot (see DEPLOYMENT.md)
sudo systemctl enable para-organizer
```

---

## 🐛 Troubleshooting

```bash
# Check if running
bash deploy.sh status

# View logs
bash deploy.sh logs

# Restart
bash deploy.sh restart

# Test API
bash deploy.sh test

# Backup before any changes
bash deploy.sh backup
```

---

## 📚 Full Documentation

See **DEPLOYMENT.md** (14KB) for:
- Prerequisites & installation
- Docker Compose architecture
- Common commands
- Production deployment on Contabo
- Nginx SSL setup
- Monitoring & logging
- Troubleshooting
- Performance tuning
- Security hardening
- Automated backups

---

## ✨ Next Steps

1. ✅ Run `bash deploy.sh up`
2. ✅ Open http://localhost:8731
3. ✅ Add your first note
4. 📖 Read DEPLOYMENT.md for production setup
5. 🔐 Setup HTTPS + Telegram bot
6. 📊 Monitor logs regularly

---

**Ready to deploy?** → `bash deploy.sh up` 🚀
