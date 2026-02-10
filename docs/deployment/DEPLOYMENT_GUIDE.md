# Deployment Guide - Simple & Clear

## 📁 One File for Everything: docker-compose.yml

You have **ONE docker-compose.yml** that works for both development and production.
Control behavior with the `API_WORKERS` environment variable.

---

## 💻 Development (MacBook)

### Use 2 workers (saves RAM, cooler laptop)

**Option 1: Environment variable**
```bash
API_WORKERS=2 docker-compose up -d
```

**Option 2: Set in .env file**
```bash
echo "API_WORKERS=2" >> .env
docker-compose up -d
```

**Memory usage with 2 workers: ~1.5-2GB total**

---

## 🚀 Production (8GB Server)

### Use 4 workers (default, handles 80-120 req/sec)

```bash
# Just run docker-compose - defaults to 4 workers
docker-compose up -d
```

Or explicitly set in .env:
```bash
echo "API_WORKERS=4" >> .env
docker-compose up -d
```

**Memory usage with 4 workers: ~3-4GB total (perfect for 8GB server)**

---

## 🔧 Quick Commands

### Start Services
```bash
# Development
API_WORKERS=2 docker-compose up -d

# Production (or just docker-compose up -d)
docker-compose up -d
```

### Check Status
```bash
docker-compose ps
docker stats
```

### View Logs
```bash
docker-compose logs -f api
docker-compose logs --tail=100 worker
```

### Stop Services
```bash
docker-compose down
```

### Restart After Code Changes
```bash
docker-compose restart api
```

---

## 🗄️ Database Access

### Option 1: Adminer (Lightweight Web GUI - 128MB)

```bash
# Start Adminer
docker-compose --profile tools up -d adminer

# Open browser: http://localhost:8080
# Login:
#   System: PostgreSQL
#   Server: db
#   Username: luran_user
#   Password: (from .env POSTGRES_PASSWORD)
#   Database: luran_db

# Stop when done
docker-compose stop adminer
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

# Install Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

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
# Build and start
docker-compose up -d

# Check status
docker-compose ps

# Run migrations
docker-compose exec api alembic upgrade head

# Test health
curl http://localhost:8000/api/v1/health

# Seed database (create subscription plans + platform admin)
docker-compose exec api python scripts/seed_data.py
```

### 4. Setup Automatic Database Backups

**IMPORTANT:** Set this up right after deployment!

```bash
# Make backup script executable
chmod +x scripts/backup_db.sh

# Test the backup script
./scripts/backup_db.sh

# Verify backup was created
ls -lh backups/

# Setup crontab for automatic daily backups at 2:00 AM
crontab -e

# Add this line to crontab (adjust path to your installation):
0 2 * * * cd /opt/luran && ./scripts/backup_db.sh >> ./backups/backup.log 2>&1

# Save and exit (Ctrl+X, then Y, then Enter)
```

**What this does:**
- ✅ Runs backup automatically every day at 2:00 AM
- ✅ Creates compressed backup: `luran_backup_YYYYMMDD_HHMMSS.sql.gz`
- ✅ Deletes backups older than 30 days (configurable in script)
- ✅ Logs everything to `backups/backup.log`
- ✅ Zero manual intervention needed!

**Verify crontab is set:**
```bash
crontab -l | grep luran
# Should show: 0 2 * * * cd /opt/luran && ./scripts/backup_db.sh >> ./backups/backup.log 2>&1
```

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

# Test cloud backup
./scripts/backup_db.sh
# Should upload to Google Drive
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
    server_name yourdomain.com;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
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
sudo certbot --nginx -d yourdomain.com
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
docker-compose logs --tail=100 -f api

# All services
docker-compose logs -f

# Database logs (for slow queries)
docker-compose logs --tail=50 db
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
API_WORKERS=3 docker-compose restart api

# Or in .env
echo "API_WORKERS=3" >> .env
docker-compose restart api
```

### Rate Limiting Not Working
```bash
# Check Redis is running
docker-compose ps redis

# Check if keys exist after making requests
docker exec -it luran_redis redis-cli KEYS "http_*"

# Restart API
docker-compose restart api
```

### Database Connection Errors
```bash
# Check DB is running
docker-compose ps db

# Check logs
docker-compose logs db

# Restart DB
docker-compose restart db
```

---

## 🔄 Updates

```bash
# Pull latest code
git pull origin main

# Rebuild and restart
docker-compose build
docker-compose up -d

# Run migrations
docker-compose exec api alembic upgrade head

# Check health
curl http://localhost:8000/api/v1/health
```

---

## 📊 Quick Reference

| Task | Command |
|------|---------|
| Start dev (2 workers) | `API_WORKERS=2 docker-compose up -d` |
| Start prod (4 workers) | `docker-compose up -d` |
| Stop all | `docker-compose down` |
| View logs | `docker-compose logs -f api` |
| Check resources | `docker stats` |
| Database GUI | `docker-compose --profile tools up -d adminer` |
| Database CLI | `docker exec -it luran_db psql -U luran_user -d luran_db` |
| Restart API | `docker-compose restart api` |
| Run migrations | `docker-compose exec api alembic upgrade head` |

---

## ✅ What's Included

The docker-compose.yml has everything configured:

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
| 8GB | 4 | 200-500 users |
| 16GB+ | 6-8 | 500+ users |

**Start with 4 workers on your 8GB server. Scale up only if needed.**

---

## Summary

🎯 **One file for everything:** `docker-compose.yml`
🎯 **Development:** `API_WORKERS=2 docker-compose up -d`
🎯 **Production:** `docker-compose up -d` (defaults to 4 workers)
🎯 **Database GUI:** `docker-compose --profile tools up -d adminer`
🎯 **All optimized:** Resource limits, health checks, log rotation included

**That's it! Keep it simple.** 🚀
