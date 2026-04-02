#!/bin/bash

# ============================================================================
# Luran Database Backup Script
# ============================================================================
# This script creates PostgreSQL backups and optionally uploads to cloud storage
# Supports: Local storage, AWS S3, Google Drive, Dropbox
# ============================================================================

set -euo pipefail  # Exit on error and fail on unset vars

# ============================================================================
# Load Environment Variables
# ============================================================================
# Load .env file if it exists
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# ============================================================================
# Configuration
# ============================================================================
BACKUP_DIR="./backups"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-luran}"
DB_CONTAINER="${DB_CONTAINER:-${COMPOSE_PROJECT_NAME}_db}"
DB_USER="${POSTGRES_USER}"  # From .env file
DB_NAME="${POSTGRES_DB}"    # From .env file
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
DATE_PATH=$(date +"%Y/%m")
BACKUP_FILE="luran_backup_${TIMESTAMP}.sql"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_FILE}"

# Retention settings (keep backups for N days)
RETENTION_DAYS="${RETENTION_DAYS:-30}"

# Cloud storage settings (from environment variables)
ENABLE_CLOUD_BACKUP=${ENABLE_CLOUD_BACKUP:-false}
CLOUD_PROVIDER=${CLOUD_PROVIDER:-""}  # Options: s3, gdrive, dropbox

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
# Main Backup Process
# ============================================================================
echo ""
echo "🗄️  ============================================"
echo "🗄️  Luran Database Backup"
echo "🗄️  ============================================"
log_info "Timestamp: ${TIMESTAMP}"
log_info "Database: ${DB_NAME}"
log_info "Container: ${DB_CONTAINER}"
echo ""

# Create backups directory if it doesn't exist
mkdir -p "${BACKUP_DIR}"

# Check if Docker container is running
if ! docker ps | grep -q ${DB_CONTAINER}; then
    log_error "Database container '${DB_CONTAINER}' is not running!"
    exit 1
fi

if [ -z "${DB_USER:-}" ] || [ -z "${DB_NAME:-}" ]; then
    log_error "POSTGRES_USER and POSTGRES_DB must be set in .env"
    exit 1
fi

# Create backup using pg_dump inside Docker container
log_info "Creating backup: ${BACKUP_FILE}"
if docker exec ${DB_CONTAINER} pg_dump -U ${DB_USER} -d ${DB_NAME} > "${BACKUP_PATH}"; then
    log_success "Backup created successfully"
else
    log_error "Failed to create backup!"
    exit 1
fi

# Compress the backup
log_info "Compressing backup..."
gzip "${BACKUP_PATH}"
BACKUP_PATH="${BACKUP_PATH}.gz"

# Get backup size
BACKUP_SIZE=$(du -h "${BACKUP_PATH}" | cut -f1)
log_success "Backup compressed: ${BACKUP_FILE}.gz (${BACKUP_SIZE})"

# ============================================================================
# Cloud Upload (Optional)
# ============================================================================
if [ "$ENABLE_CLOUD_BACKUP" = "true" ]; then
    echo ""
    log_info "Cloud backup enabled: ${CLOUD_PROVIDER}"

    case "$CLOUD_PROVIDER" in
        "s3")
            # AWS S3 Upload
            if [ -z "$AWS_S3_BUCKET" ]; then
                log_error "AWS_S3_BUCKET not set in environment variables"
            else
                log_info "Uploading to S3: s3://${AWS_S3_BUCKET}/backups/${DATE_PATH}/"

                if command -v aws &> /dev/null; then
                    aws s3 cp "${BACKUP_PATH}" "s3://${AWS_S3_BUCKET}/backups/${DATE_PATH}/${BACKUP_FILE}.gz"
                    log_success "Uploaded to AWS S3"
                else
                    log_warning "AWS CLI not installed. Skipping S3 upload."
                    log_info "Install: pip install awscli"
                fi
            fi
            ;;

        "gdrive")
            # Google Drive Upload (using rclone)
            if [ -z "$GDRIVE_FOLDER" ]; then
                log_error "GDRIVE_FOLDER not set in environment variables"
            else
                log_info "Uploading to Google Drive: ${GDRIVE_FOLDER}/${DATE_PATH}/"

                if command -v rclone &> /dev/null; then
                    # Create folder structure in Google Drive
                    rclone mkdir "gdrive:${GDRIVE_FOLDER}/${DATE_PATH}"

                    # Upload backup
                    rclone copy "${BACKUP_PATH}" "gdrive:${GDRIVE_FOLDER}/${DATE_PATH}/" --progress
                    log_success "Uploaded to Google Drive"
                else
                    log_warning "rclone not installed. Skipping Google Drive upload."
                    log_info "Install: brew install rclone (macOS) or apt install rclone (Linux)"
                    log_info "Setup: rclone config (choose Google Drive)"
                fi
            fi
            ;;

        "dropbox")
            # Dropbox Upload (using rclone)
            if [ -z "$DROPBOX_FOLDER" ]; then
                log_error "DROPBOX_FOLDER not set in environment variables"
            else
                log_info "Uploading to Dropbox: ${DROPBOX_FOLDER}/${DATE_PATH}/"

                if command -v rclone &> /dev/null; then
                    # Create folder structure in Dropbox
                    rclone mkdir "dropbox:${DROPBOX_FOLDER}/${DATE_PATH}"

                    # Upload backup
                    rclone copy "${BACKUP_PATH}" "dropbox:${DROPBOX_FOLDER}/${DATE_PATH}/" --progress
                    log_success "Uploaded to Dropbox"
                else
                    log_warning "rclone not installed. Skipping Dropbox upload."
                    log_info "Install: brew install rclone (macOS) or apt install rclone (Linux)"
                    log_info "Setup: rclone config (choose Dropbox)"
                fi
            fi
            ;;

        *)
            log_warning "Unknown cloud provider: ${CLOUD_PROVIDER}"
            log_info "Supported providers: s3, gdrive, dropbox"
            ;;
    esac
fi

# ============================================================================
# Cleanup Old Backups
# ============================================================================
echo ""
log_info "Cleaning up old backups (older than ${RETENTION_DAYS} days)..."
DELETED_COUNT=$(find "${BACKUP_DIR}" -name "luran_backup_*.sql.gz" -mtime +${RETENTION_DAYS} -delete -print | wc -l)

if [ "$DELETED_COUNT" -gt 0 ]; then
    log_success "Deleted ${DELETED_COUNT} old backup(s)"
else
    log_info "No old backups to delete"
fi

# Count remaining backups
BACKUP_COUNT=$(ls -1 "${BACKUP_DIR}"/luran_backup_*.sql.gz 2>/dev/null | wc -l | tr -d ' ')
log_info "Total local backups: ${BACKUP_COUNT}"

# ============================================================================
# Summary
# ============================================================================
echo ""
echo "✨ ============================================"
echo "✨ Backup Completed Successfully!"
echo "✨ ============================================"
echo "📍 Local: ${BACKUP_PATH}"
echo "📊 Size: ${BACKUP_SIZE}"
echo "🗓️  Retention: ${RETENTION_DAYS} days"

if [ "$ENABLE_CLOUD_BACKUP" = "true" ]; then
    echo "☁️  Cloud: ${CLOUD_PROVIDER}"
fi

echo ""
