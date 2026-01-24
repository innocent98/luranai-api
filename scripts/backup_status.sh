#!/bin/bash

# ============================================================================
# Luran Backup Status Script
# ============================================================================
# Quickly check the status of your database backups
# ============================================================================

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo ""
echo "🗄️  ============================================"
echo "🗄️  Luran Backup Status"
echo "🗄️  ============================================"
echo ""

# Check cron job
echo -e "${BLUE}📅 Automated Backup Schedule:${NC}"
if crontab -l 2>/dev/null | grep -q "backup_db.sh"; then
    echo -e "${GREEN}✅ Active${NC}"
    crontab -l | grep "backup_db.sh"
else
    echo -e "${YELLOW}⚠️  No cron job found${NC}"
fi
echo ""

# Local backups
echo -e "${BLUE}📁 Local Backups:${NC}"
if ls backups/*.sql.gz 1> /dev/null 2>&1; then
    BACKUP_COUNT=$(ls -1 backups/*.sql.gz | wc -l | tr -d ' ')
    TOTAL_SIZE=$(du -sh backups/ | cut -f1)
    echo -e "${GREEN}✅ ${BACKUP_COUNT} backups (${TOTAL_SIZE})${NC}"
    echo ""
    echo "Recent backups:"
    ls -lht backups/*.sql.gz | head -5 | awk '{print "  " $9 " (" $5 ", " $6 " " $7 " " $8 ")"}'
else
    echo -e "${YELLOW}⚠️  No local backups found${NC}"
fi
echo ""

# Google Drive backups
echo -e "${BLUE}☁️  Google Drive Backups:${NC}"
if command -v rclone &> /dev/null; then
    if rclone listremotes | grep -q "gdrive:"; then
        GDRIVE_COUNT=$(rclone ls gdrive:LuranBackups/ 2>/dev/null | wc -l | tr -d ' ')
        if [ "$GDRIVE_COUNT" -gt 0 ]; then
            echo -e "${GREEN}✅ ${GDRIVE_COUNT} backups in Google Drive${NC}"
            echo ""
            echo "Recent cloud backups:"
            rclone ls gdrive:LuranBackups/2026/01/ 2>/dev/null | tail -3 | awk '{printf "  %s (%.0f KB)\n", $2, $1/1024}'
        else
            echo -e "${YELLOW}⚠️  No backups in Google Drive yet${NC}"
        fi
    else
        echo -e "${YELLOW}⚠️  Google Drive not configured${NC}"
    fi
else
    echo -e "${YELLOW}⚠️  rclone not installed${NC}"
fi
echo ""

# Last backup log
echo -e "${BLUE}📝 Last Backup Log:${NC}"
if [ -f backups/backup.log ]; then
    echo "Last 10 lines:"
    tail -10 backups/backup.log | grep -E "✅|❌|Backup Completed|Error" || echo "  (No recent activity)"
else
    echo -e "${YELLOW}⚠️  No log file found${NC}"
fi
echo ""

# Next backup time
echo -e "${BLUE}⏰ Next Scheduled Backup:${NC}"
if crontab -l 2>/dev/null | grep -q "2 \* \* \*"; then
    CURRENT_HOUR=$(date +%H)
    if [ "$CURRENT_HOUR" -lt 2 ]; then
        echo "  Today at 2:00 AM"
    else
        echo "  Tomorrow at 2:00 AM"
    fi
else
    echo "  (No schedule configured)"
fi
echo ""

echo "============================================"
echo ""
