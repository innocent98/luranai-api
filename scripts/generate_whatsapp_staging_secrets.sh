#!/usr/bin/env bash
# Generates the WhatsApp staging env block into a LOCAL file (mode 0600) and prints NOTHING on success.
# You then open the file yourself, paste the lines into .env.staging, re-encrypt (scripts/env.sh encrypt staging) and
# delete the file. The values never appear in a terminal, a shell history or a chat.
#
#   scripts/generate_whatsapp_staging_secrets.sh [output-file]      # default: ./.whatsapp-staging-secrets.env
#
# Refuses to overwrite an existing file. The default name is git-ignored. Secrets are hex (URL- and shell-safe) and meet the
# sidecar's rules: token/secret >= 32 chars, master key >= 64 hex chars, DB password alphanumeric >= 24 chars.
#
# BACK UP WHATSAPP_AUTH_MASTER_KEY in your password manager before deploying: losing or changing it makes every stored
# WhatsApp credential undecryptable and every linked number must re-pair.
set -euo pipefail
umask 077

out="${1:-.whatsapp-staging-secrets.env}"
if [ -e "$out" ]; then
  echo "refusing to overwrite existing file: $out" >&2
  exit 1
fi
command -v openssl >/dev/null 2>&1 || { echo "openssl not found" >&2; exit 1; }

tmp="$(mktemp "${out}.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

{
  echo "# Generated $(date -u +%Y-%m-%dT%H:%M:%SZ). Paste into .env.staging, re-encrypt, then delete this file."
  echo "# Backend + sidecar (shared):"
  echo "WHATSAPP_ENABLED=true"
  echo "WHATSAPP_SIDECAR_URL=http://whatsapp-sidecar:8787"
  echo "WHATSAPP_MAX_CONNECTIONS=2"
  echo "WHATSAPP_SIDECAR_TOKEN=$(openssl rand -hex 32)"
  echo "WHATSAPP_WEBHOOK_SECRET=$(openssl rand -hex 32)"
  echo "# Backend only:"
  echo "WHATSAPP_CONTACT_PEPPER=$(openssl rand -hex 32)"
  echo "# Sidecar only (CD moves these out of the api/worker env file):"
  echo "WHATSAPP_AUTH_MASTER_KEY=$(openssl rand -hex 32)"
  echo "WHATSAPP_SIDECAR_DB_PASSWORD=$(openssl rand -hex 24)"
} > "$tmp"

chmod 600 "$tmp"
mv "$tmp" "$out"
trap - EXIT
