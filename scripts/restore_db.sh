#!/bin/bash

# ============================================================================
# Luran Database Restore Script
# ============================================================================
# This script restores PostgreSQL database from a backup file
# ============================================================================

set -e  # Exit on error

# ============================================================================
# Configuration
# ============================================================================
DB_CONTAINER="luran_db"
DB_USER="user"
DB_NAME="luran_db"

# ============================================================================
# Colors for output
# ============================================================================
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# ============================================================================
# Helper Functions
# ============================================================================
log_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

log_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

log_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

log_error() {
    echo -e "${RED}❌ $1${NC}"
}

# ============================================================================
# Validation
# ============================================================================
if [ -z "$1" ]; then
    echo ""
    log_error "Usage: ./restore_db.sh <backup_file.sql.gz>"
    echo ""
    log_info "Available backups:"
    echo ""
    if ls ./backups/*.sql.gz 1> /dev/null 2>&1; then
        ls -lh ./backups/*.sql.gz | awk '{print "  " $9 " (" $5 ")"}'
    else
        echo "  No backups found in ./backups/"
    fi
    echo ""
    exit 1
fi

BACKUP_FILE="$1"

if [ ! -f "$BACKUP_FILE" ]; then
    log_error "Backup file not found: $BACKUP_FILE"
    exit 1
fi

# ============================================================================
# Confirmation
# ============================================================================
echo ""
echo "🔄 ============================================"
echo "🔄 Luran Database Restore"
echo "🔄 ============================================"
echo ""
log_warning "WARNING: This will OVERWRITE the current database!"
log_warning "All existing data will be PERMANENTLY DELETED!"
echo ""
log_info "Database: ${DB_NAME}"
log_info "Backup file: ${BACKUP_FILE}"
echo ""
read -p "Are you sure you want to continue? Type 'yes' to proceed: " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    log_error "Restore cancelled by user"
    exit 0
fi

# ============================================================================
# Pre-Restore Backup
# ============================================================================
echo ""
log_info "Creating safety backup of current database..."
SAFETY_BACKUP="./backups/pre_restore_backup_$(date +%Y%m%d_%H%M%S).sql.gz"

docker exec -t ${DB_CONTAINER} pg_dump -U ${DB_USER} -d ${DB_NAME} | gzip > "${SAFETY_BACKUP}"
log_success "Safety backup created: ${SAFETY_BACKUP}"

# ============================================================================
# Restore Process
# ============================================================================
echo ""
log_info "Starting database restore..."

# Check if Docker container is running
if ! docker ps | grep -q ${DB_CONTAINER}; then
    log_error "Database container '${DB_CONTAINER}' is not running!"
    exit 1
fi

# Decompress if gzipped
TEMP_FILE="/tmp/luran_restore_temp_$$.sql"
if [[ $BACKUP_FILE == *.gz ]]; then
    log_info "Decompressing backup..."
    gunzip -c "$BACKUP_FILE" > "$TEMP_FILE"
else
    TEMP_FILE="$BACKUP_FILE"
fi

# Drop existing connections
log_info "Terminating existing database connections..."
docker exec -t ${DB_CONTAINER} psql -U ${DB_USER} -d postgres -c "SELECT pg_terminate_backend(pg_stat_activity.pid) FROM pg_stat_activity WHERE pg_stat_activity.datname = '${DB_NAME}' AND pid <> pg_backend_pid();" > /dev/null 2>&1 || true

# Drop and recreate database
log_info "Recreating database..."
docker exec -t ${DB_CONTAINER} psql -U ${DB_USER} -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME};"
docker exec -t ${DB_CONTAINER} psql -U ${DB_USER} -d postgres -c "CREATE DATABASE ${DB_NAME};"

# Restore from backup
log_info "Restoring data..."
cat "$TEMP_FILE" | docker exec -i ${DB_CONTAINER} psql -U ${DB_USER} -d ${DB_NAME} > /dev/null 2>&1

# Clean up temp file if we decompressed
if [[ $BACKUP_FILE == *.gz ]]; then
    rm -f "$TEMP_FILE"
fi

log_success "Database restored successfully!"

# ============================================================================
# Post-Restore Actions
# ============================================================================
echo ""
log_info "Restarting API container..."
if docker restart luran_api > /dev/null 2>&1; then
    log_success "API container restarted"
else
    log_warning "Could not restart API container (may not be running)"
fi

# Restart worker if it exists
if docker ps -a | grep -q luran_worker; then
    log_info "Restarting worker container..."
    docker restart luran_worker > /dev/null 2>&1 || true
    log_success "Worker container restarted"
fi

# ============================================================================
# Summary
# ============================================================================
echo ""
echo "✨ ============================================"
echo "✨ Restore Completed Successfully!"
echo "✨ ============================================"
log_info "Database: ${DB_NAME}"
log_info "Restored from: ${BACKUP_FILE}"
log_info "Safety backup: ${SAFETY_BACKUP}"
echo ""
log_warning "Remember to verify your application is working correctly!"
echo ""
