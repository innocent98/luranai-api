# 🗄️ Luran Database Backup System

Complete guide for backing up and restoring your Luran database with support for local and cloud storage.

## Which script does what?

| Script | Audience | Where it runs |
|--------|----------|---------------|
| `backup_db.sh` | **Developers** — manual/ad-hoc backups during local dev | Your laptop / dev machine |
| `backup_cron.sh` | **Production** — automated daily backups installed by CD | VPS host (via cron, 2 AM daily) |
| `backup_status.sh` | Operators — check last backup status | Either environment |

The production backup cron is installed automatically by the CD pipeline (`.github/workflows/cd.yml`) — you don't need to set it up manually. This document covers the developer workflow (`backup_db.sh`).

---

## 📋 Table of Contents

1. [Quick Start](#quick-start)
2. [Manual Backup](#manual-backup)
3. [Automated Backups](#automated-backups)
4. [Cloud Storage Setup](#cloud-storage-setup)
5. [Restore Database](#restore-database)
6. [Troubleshooting](#troubleshooting)

---

## 🚀 Quick Start

### Create a Backup (Local Only)

```bash
# From project root
./scripts/backup_db.sh
```

**Output:**
- Backup file saved to: `./backups/luran_backup_YYYYMMDD_HHMMSS.sql.gz`
- Automatically compressed with gzip
- Old backups (>30 days) automatically deleted

---

## 💾 Manual Backup

### Local Backup

```bash
# Navigate to project root
cd /Users/adebayovictor/Documents/dev/luran/luran

# Run backup script
./scripts/backup_db.sh
```

**What happens:**
1. ✅ Creates SQL dump of database
2. ✅ Compresses with gzip
3. ✅ Saves to `./backups/` directory
4. ✅ Deletes backups older than 30 days
5. ✅ Shows backup size and location

**Backup Location:**
```
/Users/adebayovictor/Documents/dev/luran/luran/backups/
├── luran_backup_20260124_140530.sql.gz
├── luran_backup_20260123_140530.sql.gz
└── luran_backup_20260122_140530.sql.gz
```

---

## ⏰ Automated Backups

### Option 1: Cron Job (Recommended)

**Setup daily backups at 2 AM:**

```bash
# Open crontab editor
crontab -e

# Add this line (adjust path if needed)
0 2 * * * cd /Users/adebayovictor/Documents/dev/luran/luran && ./scripts/backup_db.sh >> ./backups/backup.log 2>&1
```

**Cron Schedule Examples:**
```bash
# Daily at 2 AM
0 2 * * * cd /path/to/luran && ./scripts/backup_db.sh

# Every 6 hours
0 */6 * * * cd /path/to/luran && ./scripts/backup_db.sh

# Every Sunday at 3 AM
0 3 * * 0 cd /path/to/luran && ./scripts/backup_db.sh

# Twice daily (6 AM and 6 PM)
0 6,18 * * * cd /path/to/luran && ./scripts/backup_db.sh
```

**Verify cron job:**
```bash
# List active cron jobs
crontab -l

# View backup logs
tail -f ./backups/backup.log
```

### Option 2: macOS launchd (Alternative to cron)

Create `~/Library/LaunchAgents/com.luran.backup.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.luran.backup</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/adebayovictor/Documents/dev/luran/luran/scripts/backup_db.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>2</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>/Users/adebayovictor/Documents/dev/luran/luran</string>
    <key>StandardOutPath</key>
    <string>/Users/adebayovictor/Documents/dev/luran/luran/backups/backup.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/adebayovictor/Documents/dev/luran/luran/backups/backup.log</string>
</dict>
</plist>
```

**Load the job:**
```bash
launchctl load ~/Library/LaunchAgents/com.luran.backup.plist
```

---

## ☁️ Cloud Storage Setup

### Google Drive (Recommended)

**1. Install rclone:**
```bash
# macOS
brew install rclone

# Linux
sudo apt install rclone
```

**2. Configure rclone for Google Drive:**
```bash
rclone config

# Follow prompts:
# - Choose "n" for new remote
# - Name: gdrive
# - Storage: Google Drive (option 15)
# - Client ID: (leave blank or use your own)
# - Client Secret: (leave blank or use your own)
# - Scope: Full access (option 1)
# - Root folder: (leave blank)
# - Service account: No
# - Auto config: Yes
# - Browser will open for Google authentication
# - Choose your Google account and allow access
```

**3. Configure environment variables:**

Add to `.env` file:
```bash
ENABLE_CLOUD_BACKUP=true
CLOUD_PROVIDER=gdrive
GDRIVE_FOLDER=LuranBackups
```

**4. Test backup:**
```bash
./scripts/backup_db.sh
```

**Verify upload:**
```bash
# List files in Google Drive
rclone ls gdrive:LuranBackups/
```

---

### Dropbox

**1. Install rclone:**
```bash
# macOS
brew install rclone

# Linux
sudo apt install rclone
```

**2. Configure rclone for Dropbox:**
```bash
rclone config

# Follow prompts:
# - Choose "n" for new remote
# - Name: dropbox
# - Storage: Dropbox (option 11)
# - Client ID: (leave blank)
# - Client Secret: (leave blank)
# - Auto config: Yes
# - Browser will open for Dropbox authentication
# - Log in and allow access
```

**3. Configure environment variables:**

Add to `.env` file:
```bash
ENABLE_CLOUD_BACKUP=true
CLOUD_PROVIDER=dropbox
DROPBOX_FOLDER=LuranBackups
```

**4. Test backup:**
```bash
./scripts/backup_db.sh
```

**Verify upload:**
```bash
# List files in Dropbox
rclone ls dropbox:LuranBackups/
```

---

### Backblaze B2

Backblaze B2 is the cheapest durable object storage we use — roughly 1/4 the price of S3, and free egress to Cloudflare via the Bandwidth Alliance. Same rclone pattern as Google Drive/Dropbox.

**1. Install rclone** (same as above — skip if done).

**2. Create a B2 application key:**

In the Backblaze console: **Account → Application Keys → "Add a New Application Key"**. Write down the `keyID` and `applicationKey`.

**3. Configure rclone for B2:**

```bash
rclone config

# Follow prompts:
# - Choose "n" for new remote
# - Name: b2
# - Storage: Backblaze B2 (option varies by rclone version — search for "b2")
# - Account ID / key ID: paste the keyID
# - Application key: paste the applicationKey
# - Endpoint: leave blank
# - Edit advanced config: No
```

**4. Create the bucket (via rclone or the console):**

```bash
rclone mkdir b2:luran-backups
```

**5. Configure environment variables:**

Add to `.env` file:
```bash
ENABLE_CLOUD_BACKUP=true
CLOUD_PROVIDER=b2
B2_BUCKET=luran-backups
# Optional — override if your rclone remote isn't named "b2":
# B2_RCLONE_REMOTE=backblaze
```

**6. Test backup:**

```bash
./scripts/backup_db.sh
```

**Verify upload:**

```bash
rclone ls b2:luran-backups/
```

**Lifecycle rules (recommended):** In the Backblaze console, set a bucket-level lifecycle policy to hide files after N days and delete after M days. This complements the script's local `RETENTION_DAYS` — use both, since the script can only prune what's on the host.

---

### AWS S3

**1. Install AWS CLI:**
```bash
# macOS
brew install awscli

# Linux
sudo apt install awscli
```

**2. Configure AWS credentials:**
```bash
aws configure

# Enter:
# - AWS Access Key ID
# - AWS Secret Access Key
# - Default region (e.g., us-east-1)
# - Output format: json
```

**3. Create S3 bucket:**
```bash
aws s3 mb s3://luran-db-backups
```

**4. Configure environment variables:**

Add to `.env` file:
```bash
ENABLE_CLOUD_BACKUP=true
CLOUD_PROVIDER=s3
AWS_S3_BUCKET=luran-db-backups
AWS_REGION=us-east-1
```

**5. Test backup:**
```bash
./scripts/backup_db.sh
```

**Verify upload:**
```bash
aws s3 ls s3://luran-db-backups/backups/
```

---

## 🔄 Restore Database

### List Available Backups

```bash
./scripts/restore_db.sh

# Output shows available backups:
# Available backups:
#   ./backups/luran_backup_20260124_140530.sql.gz (1.2M)
#   ./backups/luran_backup_20260123_140530.sql.gz (1.1M)
```

### Restore from Backup

```bash
./scripts/restore_db.sh ./backups/luran_backup_20260124_140530.sql.gz
```

**What happens:**
1. ⚠️  Confirmation prompt (type `yes` to proceed)
2. ✅ Creates safety backup of current database
3. ✅ Terminates active connections
4. ✅ Drops existing database
5. ✅ Creates new database
6. ✅ Restores data from backup
7. ✅ Restarts API and worker containers

**Safety Features:**
- Creates pre-restore backup automatically
- Requires explicit confirmation
- Preserves current database before restore

---

### Restore from Cloud

**Download from Google Drive:**
```bash
# List backups
rclone ls gdrive:LuranBackups/2026/01/

# Download specific backup
rclone copy gdrive:LuranBackups/2026/01/luran_backup_20260124_140530.sql.gz ./backups/

# Restore
./scripts/restore_db.sh ./backups/luran_backup_20260124_140530.sql.gz
```

**Download from Dropbox:**
```bash
# List backups
rclone ls dropbox:LuranBackups/2026/01/

# Download specific backup
rclone copy dropbox:LuranBackups/2026/01/luran_backup_20260124_140530.sql.gz ./backups/

# Restore
./scripts/restore_db.sh ./backups/luran_backup_20260124_140530.sql.gz
```

**Download from AWS S3:**
```bash
# List backups
aws s3 ls s3://luran-db-backups/backups/2026/01/

# Download specific backup
aws s3 cp s3://luran-db-backups/backups/2026/01/luran_backup_20260124_140530.sql.gz ./backups/

# Restore
./scripts/restore_db.sh ./backups/luran_backup_20260124_140530.sql.gz
```

**Download from Backblaze B2:**
```bash
# List backups
rclone ls b2:luran-backups/2026/01/

# Download specific backup
rclone copy b2:luran-backups/2026/01/luran_backup_20260124_140530.sql.gz ./backups/

# Restore
./scripts/restore_db.sh ./backups/luran_backup_20260124_140530.sql.gz
```

---

## 🔍 Troubleshooting

### Backup Failed: Container Not Running

**Error:**
```
❌ Database container 'luran_db' is not running!
```

**Solution:**
```bash
# Start Docker containers
docker-compose up -d

# Verify containers are running
docker ps
```

---

### Permission Denied

**Error:**
```
bash: ./scripts/backup_db.sh: Permission denied
```

**Solution:**
```bash
chmod +x ./scripts/backup_db.sh
chmod +x ./scripts/restore_db.sh
```

---

### Disk Space Full

**Error:**
```
gzip: stdout: No space left on device
```

**Solution:**
```bash
# Check disk space
df -h

# Delete old backups manually
rm ./backups/luran_backup_2025*.sql.gz

# Adjust retention days in backup script
# Edit RETENTION_DAYS variable
```

---

### Cloud Upload Failed: rclone Not Found

**Error:**
```
⚠️  rclone not installed. Skipping Google Drive upload.
```

**Solution:**
```bash
# macOS
brew install rclone

# Linux
sudo apt install rclone

# Configure
rclone config
```

---

### Cloud Upload Failed: Authentication

**Error:**
```
Failed to copy: directory not found
```

**Solution:**
```bash
# Reconfigure rclone
rclone config reconnect gdrive:

# Or delete and recreate remote
rclone config delete gdrive
rclone config  # Create new remote
```

---

## 📊 Backup Best Practices

### Development

✅ **Local backups only**
- Daily automated backups
- 30-day retention
- Before major migrations

### Staging

✅ **Local + Cloud (Google Drive/Dropbox)**
- Daily automated backups
- 60-day retention
- Before deployments

### Production

✅ **Local + Cloud (AWS S3 or Google Cloud)**
- Multiple daily backups (every 6 hours)
- 90-day cloud retention
- Before every deployment
- Before every migration
- Weekly full backups

---

## 🗓️ Backup Schedule Recommendations

| Environment | Frequency | Retention | Cloud Storage |
|------------|-----------|-----------|---------------|
| **Development** | Daily | 30 days | Optional |
| **Staging** | Daily | 60 days | Recommended |
| **Production** | Every 6 hours | 90 days | Required |

---

## 📝 Monitoring Backups

### Check Backup Logs

```bash
# View recent backup logs
tail -f ./backups/backup.log

# View last 100 lines
tail -100 ./backups/backup.log

# Search for errors
grep -i error ./backups/backup.log
```

### Verify Backups

```bash
# List all backups with sizes
ls -lh ./backups/*.sql.gz

# Count backups
ls -1 ./backups/*.sql.gz | wc -l

# Check total backup size
du -sh ./backups/
```

### Test Restore (Recommended Monthly)

```bash
# 1. Create test environment
docker-compose -f docker-compose.test.yml up -d

# 2. Restore latest backup to test database
./scripts/restore_db.sh ./backups/luran_backup_latest.sql.gz

# 3. Verify data integrity
docker exec -it luran_db psql -U user -d luran_db -c "SELECT COUNT(*) FROM tenants;"

# 4. Cleanup
docker-compose -f docker-compose.test.yml down
```

---

## 🆘 Emergency Recovery

If your primary database is corrupted or lost:

### Step 1: Download latest backup from cloud

```bash
# Google Drive
rclone copy gdrive:LuranBackups/ ./backups/ --include "*.sql.gz" --max-age 7d

# Dropbox
rclone copy dropbox:LuranBackups/ ./backups/ --include "*.sql.gz" --max-age 7d

# Backblaze B2
rclone copy b2:luran-backups/ ./backups/ --include "*.sql.gz" --max-age 7d

# AWS S3
aws s3 sync s3://luran-db-backups/backups/ ./backups/ --exclude "*" --include "*.sql.gz"
```

### Step 2: Find most recent backup

```bash
ls -lt ./backups/*.sql.gz | head -5
```

### Step 3: Restore

```bash
./scripts/restore_db.sh ./backups/luran_backup_YYYYMMDD_HHMMSS.sql.gz
```

### Step 4: Verify

```bash
# Check database
docker exec -it luran_db psql -U user -d luran_db -c "\dt"

# Verify critical tables
docker exec -it luran_db psql -U user -d luran_db -c "SELECT COUNT(*) FROM tenants;"
docker exec -it luran_db psql -U user -d luran_db -c "SELECT COUNT(*) FROM users;"
docker exec -it luran_db psql -U user -d luran_db -c "SELECT COUNT(*) FROM conversation_sessions;"
```

---

## 📞 Support

If you encounter issues:

1. Check logs: `tail -f ./backups/backup.log`
2. Verify Docker containers: `docker ps`
3. Check disk space: `df -h`
4. Test database connection: `docker exec -it luran_db psql -U user -d luran_db -c "SELECT 1;"`

---

**Last Updated:** 2026-01-24
**Version:** 1.0.0
