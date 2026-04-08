#!/usr/bin/env bash
# ============================================================
# Luran .env Encryption/Decryption Helper
#
# Encrypts .env → .env.enc (safe to commit to git)
# Decrypts .env.enc → .env (never committed)
#
# Usage:
#   ./scripts/env.sh encrypt          # .env → .env.enc
#   ./scripts/env.sh decrypt          # .env.enc → .env
#   ./scripts/env.sh rotate           # re-encrypt with a new key
#   ./scripts/env.sh verify           # check .env.enc can be decrypted
#
# The encryption key is read from:
#   1. ENV_ENCRYPTION_KEY environment variable
#   2. .env.key file (gitignored)
#   3. Interactive prompt
#
# Store the key in:
#   - GitHub Actions secret (ENV_ENCRYPTION_KEY)
#   - Your password manager
#   - An offline backup
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_ROOT/.env"
ENC_FILE="$PROJECT_ROOT/.env.enc"
KEY_FILE="$PROJECT_ROOT/.env.key"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

get_key() {
    if [ -n "${ENV_ENCRYPTION_KEY:-}" ]; then
        echo "$ENV_ENCRYPTION_KEY"
        return
    fi

    if [ -f "$KEY_FILE" ]; then
        cat "$KEY_FILE"
        return
    fi

    # Interactive prompt
    echo -e "${YELLOW}No ENV_ENCRYPTION_KEY found in environment or .env.key file.${NC}" >&2
    read -rsp "Enter encryption key: " key
    echo >&2
    echo "$key"
}

cmd_encrypt() {
    if [ ! -f "$ENV_FILE" ]; then
        echo -e "${RED}Error: $ENV_FILE not found.${NC}"
        echo "Copy your production .env file here first."
        exit 1
    fi

    local key
    key=$(get_key)
    if [ -z "$key" ]; then
        echo -e "${RED}Error: No encryption key provided.${NC}"
        exit 1
    fi

    openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
        -in "$ENV_FILE" \
        -out "$ENC_FILE" \
        -pass "pass:$key"

    echo -e "${GREEN}Encrypted:${NC} .env → .env.enc"
    echo -e "  File size: $(wc -c < "$ENC_FILE") bytes"
    echo -e "  Variables: $(grep -c '=' "$ENV_FILE" || true)"
    echo ""
    echo -e "${YELLOW}Next steps:${NC}"
    echo "  1. git add .env.enc"
    echo "  2. git commit -m 'chore: update encrypted env'"
    echo "  3. Ensure ENV_ENCRYPTION_KEY is saved in:"
    echo "     - GitHub Actions secrets"
    echo "     - Your password manager"
}

cmd_decrypt() {
    if [ ! -f "$ENC_FILE" ]; then
        echo -e "${RED}Error: $ENC_FILE not found.${NC}"
        echo "Run 'encrypt' first or pull from git."
        exit 1
    fi

    if [ -f "$ENV_FILE" ]; then
        echo -e "${YELLOW}Warning: .env already exists. Overwrite? (y/N)${NC}"
        read -r confirm
        if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
            echo "Aborted."
            exit 0
        fi
    fi

    local key
    key=$(get_key)
    if [ -z "$key" ]; then
        echo -e "${RED}Error: No encryption key provided.${NC}"
        exit 1
    fi

    if ! openssl enc -aes-256-cbc -d -pbkdf2 -iter 100000 \
        -in "$ENC_FILE" \
        -out "$ENV_FILE" \
        -pass "pass:$key" 2>/dev/null; then
        rm -f "$ENV_FILE"
        echo -e "${RED}Decryption failed. Wrong key or corrupted file.${NC}"
        exit 1
    fi

    echo -e "${GREEN}Decrypted:${NC} .env.enc → .env"
    echo -e "  Variables: $(grep -c '=' "$ENV_FILE" || true)"
}

cmd_verify() {
    if [ ! -f "$ENC_FILE" ]; then
        echo -e "${RED}Error: $ENC_FILE not found.${NC}"
        exit 1
    fi

    local key
    key=$(get_key)

    if openssl enc -aes-256-cbc -d -pbkdf2 -iter 100000 \
        -in "$ENC_FILE" \
        -pass "pass:$key" 2>/dev/null | head -1 | grep -q '='; then
        echo -e "${GREEN}Verification passed.${NC} .env.enc can be decrypted with this key."
    else
        echo -e "${RED}Verification failed.${NC} Wrong key or corrupted file."
        exit 1
    fi
}

cmd_rotate() {
    echo -e "${YELLOW}Rotating encryption key...${NC}"
    echo ""

    # Decrypt with old key
    if [ ! -f "$ENC_FILE" ]; then
        echo -e "${RED}Error: $ENC_FILE not found. Nothing to rotate.${NC}"
        exit 1
    fi

    echo "Step 1: Decrypt with current key"
    local old_key
    old_key=$(get_key)

    if ! openssl enc -aes-256-cbc -d -pbkdf2 -iter 100000 \
        -in "$ENC_FILE" \
        -out "$ENV_FILE.tmp" \
        -pass "pass:$old_key" 2>/dev/null; then
        rm -f "$ENV_FILE.tmp"
        echo -e "${RED}Decryption failed with current key.${NC}"
        exit 1
    fi

    # Generate new key
    local new_key
    new_key=$(openssl rand -hex 32)
    echo ""
    echo "Step 2: New encryption key generated"
    echo -e "${GREEN}New key:${NC} $new_key"
    echo ""

    # Encrypt with new key
    openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
        -in "$ENV_FILE.tmp" \
        -out "$ENC_FILE" \
        -pass "pass:$new_key"

    rm -f "$ENV_FILE.tmp"

    echo "Step 3: Re-encrypted .env.enc with new key"
    echo ""
    echo -e "${YELLOW}IMPORTANT — Update these locations with the new key:${NC}"
    echo "  1. GitHub Actions secret: ENV_ENCRYPTION_KEY"
    echo "  2. Your password manager"
    echo "  3. Any offline backup"
    echo ""
    echo "  git add .env.enc"
    echo "  git commit -m 'chore: rotate env encryption key'"
}

cmd_generate_key() {
    local key
    key=$(openssl rand -hex 32)
    echo -e "${GREEN}Generated key:${NC} $key"
    echo ""
    echo "Save this key to:"
    echo "  1. GitHub Actions secret → ENV_ENCRYPTION_KEY"
    echo "  2. Your password manager"
    echo ""
    echo "To save locally (gitignored):"
    echo "  echo '$key' > .env.key"
}

# ---- Main ----

case "${1:-help}" in
    encrypt)  cmd_encrypt ;;
    decrypt)  cmd_decrypt ;;
    verify)   cmd_verify ;;
    rotate)   cmd_rotate ;;
    generate-key) cmd_generate_key ;;
    help|*)
        echo "Usage: ./scripts/env.sh <command>"
        echo ""
        echo "Commands:"
        echo "  encrypt       Encrypt .env → .env.enc"
        echo "  decrypt       Decrypt .env.enc → .env"
        echo "  verify        Verify .env.enc can be decrypted"
        echo "  rotate        Re-encrypt with a new key"
        echo "  generate-key  Generate a random encryption key"
        echo ""
        echo "The encryption key is read from (in order):"
        echo "  1. ENV_ENCRYPTION_KEY environment variable"
        echo "  2. .env.key file (gitignored)"
        echo "  3. Interactive prompt"
        ;;
esac
