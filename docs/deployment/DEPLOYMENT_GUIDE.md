# Deployment Guide - Simple & Clear

## 📁 Compose Files

- `docker-compose.yml` = base (development-friendly defaults)
- `docker-compose.prod.yml` = production override

Use both files in production:

```bash
docker compose -f docker-compose.prod.yml up -d
```

Why `docker-compose.prod.yml` is shorter:
- It is an **override file**, not a full duplicate.
- Docker Compose merges it with `docker-compose.yml`.
- You only put production differences inside it (e.g., pgbouncer, no bind mounts, hidden internal ports).

---

## 💻 Development (MacBook)

### Use 2 workers (saves RAM, cooler laptop)

**Option 1: Environment variable**
```bash
API_WORKERS=2 docker compose up -d
```

**Option 2: Set in .env file**
```bash
echo "API_WORKERS=2" >> .env
docker compose up -d
```

**Memory usage with 2 workers: ~1.5-2GB total**

---

## 🚀 Production (8GB Server)

### Use 4 workers (default, handles 80-120 req/sec)

```bash
# Run with production override
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Or explicitly set in .env:
```bash
echo "API_WORKERS=4" >> .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

**Memory usage with 4 workers: ~3-4GB total (perfect for 8GB server)**

---

## 🔧 Quick Commands

### Start Services
```bash
# Development
API_WORKERS=2 docker compose up -d

# Production
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

### Check Status
```bash
docker compose ps
docker stats
```

### View Logs
```bash
docker compose logs -f api
docker compose logs --tail=100 worker
```

### Stop Services
```bash
docker compose down
```

### Restart After Code Changes
```bash
docker compose restart api
```

---

## 🗄️ Database Access

### Option 1: Adminer (Lightweight Web GUI - 128MB)

```bash
# Start Adminer
docker compose --profile tools up -d adminer

# Open browser: http://localhost:8080
# Login:
#   System: PostgreSQL
#   Server: db
#   Username: luran_user
#   Password: (from .env POSTGRES_PASSWORD)
#   Database: luran_db

# Stop when done
docker compose stop adminer
```

### Option 2: CLI (Zero RAM)

```bash
# Connect to database
docker exec -it luran_db psql -U luran_user -d luran_db

# Quick query
docker exec -it luran_db psql -U luran_user -d luran_db -c "SELECT COUNT(*) FROM tenants;"

# Backup
docker exec -t luran_db pg_dump -U luran_user luran_db > backup_$(date +%Y%m%d).sql

# Restore
docker exec -i luran_db psql -U luran_user -d luran_db < backup.sql
```

---

## 📋 Production Deployment Steps

### 1. Server Setup (Ubuntu/Debian)

```bash
# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh

# Docker Compose plugin is included with modern Docker installs.
# Verify:
docker compose version

# Clone your repo
cd /opt
git clone https://github.com/yourusername/luran.git
cd luran
```

### 2. Configure Environment

```bash
# Copy .env and update values
cp .env.example .env
nano .env

# IMPORTANT: Set strong passwords!
# Generate strong passwords:
openssl rand -hex 32  # For SECRET_KEY
openssl rand -base64 32  # For POSTGRES_PASSWORD
openssl rand -base64 24  # For FIRST_SUPERUSER_PASSWORD (12+ chars, complex!)

# Set API workers for production
echo "API_WORKERS=4" >> .env
```

### 3. Start Services

```bash
# Build and start production stack
docker compose -f docker-compose.prod.yml up -d

# Check status
docker compose -f docker-compose.prod.yml ps

# Run migrations
docker compose -f docker-compose.prod.yml exec api alembic upgrade head

# Test health
curl http://localhost:8000/api/v1/health

# Seed database (create subscription plans + platform admin)
docker compose -f docker-compose.prod.yml exec api python scripts/seed_data.py

# Seed database (create Luran support flows)
# Run part 1
docker compose -f docker-compose.prod.yml exec api \
python /app/scripts/create_luran_support_flows.py --tenant-name <TENANT_NAME>
# Run part 2
docker compose -f docker-compose.prod.yml exec api \
python /app/scripts/create_luran_support_flows_part2.py --tenant-name <TENANT_NAME>

# Seed database (create flows templates)
docker compose -f docker-compose.prod.yml exec api \
python /app/scripts/seed_flow_templates.py
```

### 4. Automatic Database Backups (installed by CD)

> **Production backups are installed automatically** by the CD pipeline
> (`.github/workflows/cd.yml`) — you don't need to set them up manually on
> the VPS. The `backup_cron.sh` script is scp'd to `/opt/luran/scripts/`
> on every deploy, and the cron entry is installed via:
>
> ```bash
> 0 2 * * * /opt/luran/scripts/backup_cron.sh >> /opt/luran/backups/backup.log 2>&1
> ```

**Verify the cron is installed on the VPS:**

```bash
crontab -l | grep backup_cron
# Should show: 0 2 * * * /opt/luran/scripts/backup_cron.sh >> /opt/luran/backups/backup.log 2>&1
```

**Run a manual backup on the VPS (to test):**

```bash
ssh <vps>
/opt/luran/scripts/backup_cron.sh
ls -lh /opt/luran/backups/
```

**Developer note:** For local/dev backups on your laptop, use `./scripts/backup_db.sh`
instead — it's a simpler wrapper that loads `.env` from the current directory.
See `scripts/README_BACKUPS.md` for details.

**What the cron does:**
- ✅ Runs every day at 2:00 AM
- ✅ Creates compressed backup: `luran_backup_YYYYMMDD_HHMMSS.sql.gz`
- ✅ Deletes backups older than 30 days
- ✅ Logs everything to `/opt/luran/backups/backup.log`
- ✅ Optional: uploads to Google Drive or S3 if `ENABLE_CLOUD_BACKUP=true` in `.env`

**Check backup logs:**
```bash
# View recent backup activity
tail -50 backups/backup.log

# List all backups
ls -lh backups/*.sql.gz
```

**Optional: Cloud Backup Setup**

If you want backups uploaded to Google Drive/S3/Dropbox:

```bash
# Edit .env file
nano .env

# Add these lines:
ENABLE_CLOUD_BACKUP=true
CLOUD_PROVIDER=gdrive  # or 's3' or 'dropbox'
GDRIVE_FOLDER=luran-backups  # Folder name in Google Drive

# For Google Drive, setup rclone (one-time):
sudo apt install rclone
rclone config  # Follow prompts to connect Google Drive

# Test cloud backup on the VPS
/opt/luran/scripts/backup_cron.sh
# Should upload to Google Drive (check backup.log)
```

### 5. Setup Nginx (Optional but Recommended)

```bash
# Install Nginx
sudo apt install nginx

# Create config
sudo nano /etc/nginx/sites-available/luran
```

Paste this config:
```nginx
server {
    listen 80;
    server_name api.luranai.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
}
```

```bash
# Enable site
sudo ln -s /etc/nginx/sites-available/luran /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### 6. SSL with Let's Encrypt

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d api.luranai.com
```

After certbot succeeds, enforce HTTPS redirect in your nginx config:

```nginx
server {
    listen 80;
    server_name api.luranai.com;
    return 301 https://$host$request_uri;
}
```

---

## 🔍 Monitoring

### Check Resources
```bash
docker stats

# Expected usage (4 workers):
# API: ~600-800MB
# Worker: ~300-400MB
# DB: ~400-600MB
# Redis: ~200MB
# Qdrant: ~400-600MB
# Total: ~3-4GB
```

### Check Rate Limiting
```bash
# View rate limit keys
docker exec -it luran_redis redis-cli KEYS "http_*"

# Check specific key
docker exec -it luran_redis redis-cli GET "http_registration:192.168.1.1"
```

### Check Logs
```bash
# API logs
docker compose logs --tail=100 -f api

# All services
docker compose logs -f

# Database logs (for slow queries)
docker compose logs --tail=50 db
```

---

## 🆘 Troubleshooting

### API Won't Start - Password Error
```
Error: Platform admin password must be at least 12 characters
```

**Fix:** Update FIRST_SUPERUSER_PASSWORD in .env:
- Must be 12+ characters
- Must NOT be common (admin, password, changethis, etc.)
- Must have 3 of 4: uppercase, lowercase, digit, special character

Example strong password: `AdminP@ssw0rd2026!`

### Memory Too High
```bash
# Check usage
docker stats

# If API using > 1GB, reduce workers
API_WORKERS=3 docker compose restart api

# Or in .env
echo "API_WORKERS=3" >> .env
docker compose restart api
```

### Rate Limiting Not Working
```bash
# Check Redis is running
docker compose ps redis

# Check if keys exist after making requests
docker exec -it luran_redis redis-cli KEYS "http_*"

# Restart API
docker compose restart api
```

### Database Connection Errors
```bash
# Check DB is running
docker compose ps db

# Check logs
docker compose logs db

# Restart DB
docker compose restart db
```

---

## 🔄 Updates

```bash
# Pull latest code
git pull origin main

# Rebuild and restart
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d

# Run migrations
docker compose -f docker-compose.prod.yml exec api alembic upgrade head

# Check health
curl http://localhost:8000/api/v1/health
```

---

## 📊 Quick Reference

| Task | Command |
|------|---------|
| Start dev (2 workers) | `API_WORKERS=2 docker compose up -d` |
| Start prod (4 workers) | `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` |
| Stop all | `docker compose down` |
| View logs | `docker compose logs -f api` |
| Check resources | `docker stats` |
| Database GUI | `docker compose --profile tools up -d adminer` |
| Database CLI | `docker exec -it luran_db psql -U luran_user -d luran_db` |
| Restart API | `docker compose restart api` |
| Run migrations (prod stack) | `docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api alembic upgrade head` |

---

## ✅ What's Included

The production stack has everything configured:

✅ **Resource Limits** - Prevents server crashes
- API: 1GB limit
- Worker: 1GB limit
- Database: 1GB limit
- Redis: 512MB limit
- Qdrant: 1GB limit

✅ **Health Checks** - Auto-restart if unhealthy
✅ **Log Rotation** - Prevents disk fill (10MB max, 3 files)
✅ **Redis Persistence** - Rate limiting data saved
✅ **Optimized PostgreSQL** - Tuned for 8GB server
✅ **Adminer** - Lightweight GUI (128MB vs PgAdmin's 500MB)

---

## 🎯 Recommendations

| Server RAM | API_WORKERS | Expected Traffic |
|------------|-------------|------------------|
| 2GB | 2 | 0-50 users |
| 4GB | 3 | 50-200 users |
| 8GB | 4 | 100-300 concurrent active sessions (after load-test) |
| 16GB+ | 6-8 | 500+ users |

**Start with 4 workers on your 8GB server. Scale up only if needed.**

---

## Summary

🎯 **Use override in production:** `docker-compose.yml` + `docker-compose.prod.yml`
🎯 **Development:** `API_WORKERS=2 docker compose up -d`
🎯 **Production:** `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d`
🎯 **Database GUI:** `docker compose --profile tools up -d adminer`
🎯 **All optimized:** Resource limits, health checks, log rotation included

**That's it! Keep it simple.** 🚀
