# 📝 Database Backup Quick Reference

> **Developer ref** — this covers `backup_db.sh` for local/manual backups.
> Production uses `backup_cron.sh` via cron (installed by CD, no manual setup needed).

## 🚀 Quick Commands

### Create Backup (local/dev)
```bash
./scripts/backup_db.sh
```

### List Backups
```bash
ls -lh ./backups/*.sql.gz
```

### Restore Backup
```bash
./scripts/restore_db.sh ./backups/luran_backup_YYYYMMDD_HHMMSS.sql.gz
```

---

## ☁️ Cloud Storage Setup

### Google Drive (5-minute setup)

```bash
# 1. Install rclone
brew install rclone  # macOS
# OR
sudo apt install rclone  # Linux

# 2. Configure
rclone config
# Choose: n → gdrive → Google Drive → (defaults) → Yes → Authenticate

# 3. Add to .env
echo "ENABLE_CLOUD_BACKUP=true" >> .env
echo "CLOUD_PROVIDER=gdrive" >> .env
echo "GDRIVE_FOLDER=LuranBackups" >> .env

# 4. Test
./scripts/backup_db.sh
```

### Dropbox (5-minute setup)

```bash
# 1. Install rclone
brew install rclone  # macOS
# OR
sudo apt install rclone  # Linux

# 2. Configure
rclone config
# Choose: n → dropbox → Dropbox → (defaults) → Yes → Authenticate

# 3. Add to .env
echo "ENABLE_CLOUD_BACKUP=true" >> .env
echo "CLOUD_PROVIDER=dropbox" >> .env
echo "DROPBOX_FOLDER=LuranBackups" >> .env

# 4. Test
./scripts/backup_db.sh
```

---

## ⏰ Automated Backups (Daily at 2 AM)

```bash
# Edit crontab
crontab -e

# Add this line
0 2 * * * cd /Users/adebayovictor/Documents/dev/luran/luran && ./scripts/backup_db.sh >> ./backups/backup.log 2>&1

# Save and exit
```

**Verify:**
```bash
crontab -l
```

---

## 🔍 Check Status

### View Backup Logs
```bash
tail -f ./backups/backup.log
```

### Count Backups
```bash
ls -1 ./backups/*.sql.gz | wc -l
```

### Total Backup Size
```bash
du -sh ./backups/
```

### List Cloud Backups
```bash
# Google Drive
rclone ls gdrive:LuranBackups/

# Dropbox
rclone ls dropbox:LuranBackups/

# AWS S3
aws s3 ls s3://luran-db-backups/backups/
```

---

## 🆘 Emergency Restore

### From Local Backup
```bash
# 1. List backups
ls -lt ./backups/*.sql.gz | head -5

# 2. Restore latest
./scripts/restore_db.sh ./backups/luran_backup_YYYYMMDD_HHMMSS.sql.gz
```

### From Google Drive
```bash
# 1. Download latest
rclone copy gdrive:LuranBackups/ ./backups/ --include "*.sql.gz" --max-age 7d

# 2. Restore
./scripts/restore_db.sh ./backups/luran_backup_YYYYMMDD_HHMMSS.sql.gz
```

### From Dropbox
```bash
# 1. Download latest
rclone copy dropbox:LuranBackups/ ./backups/ --include "*.sql.gz" --max-age 7d

# 2. Restore
./scripts/restore_db.sh ./backups/luran_backup_YYYYMMDD_HHMMSS.sql.gz
```

---

## 📍 File Locations

| Item | Location |
|------|----------|
| **Backups** | `./backups/*.sql.gz` |
| **Backup Script** | `./scripts/backup_db.sh` |
| **Restore Script** | `./scripts/restore_db.sh` |
| **Logs** | `./backups/backup.log` |
| **Config** | `.env` |

---

## ✅ Backup Checklist

### Initial Setup
- [ ] Create backup directory: `mkdir -p backups`
- [ ] Make scripts executable: `chmod +x scripts/*.sh`
- [ ] Update docker-compose.yml (already done)
- [ ] Test manual backup: `./scripts/backup_db.sh`
- [ ] Verify backup created: `ls -lh backups/`

### Cloud Setup (Optional but Recommended)
- [ ] Install rclone
- [ ] Configure cloud provider (gdrive/dropbox/s3)
- [ ] Add env variables to `.env`
- [ ] Test cloud upload
- [ ] Verify files in cloud storage

### Automation
- [ ] Set up cron job
- [ ] Test cron job runs: `tail -f backups/backup.log`
- [ ] Set calendar reminder to check backups monthly

### Monthly Maintenance
- [ ] Verify backups are running
- [ ] Check backup logs for errors
- [ ] Test restore process
- [ ] Verify cloud uploads working
- [ ] Check disk space usage

---

## 🎯 Recommended Settings

| Environment | Frequency | Retention | Cloud |
|------------|-----------|-----------|-------|
| **Dev** | Daily | 30 days | Optional |
| **Staging** | Daily | 60 days | Yes |
| **Production** | Every 6hrs | 90 days | Required |

---

Print this page and keep it handy! 📄
