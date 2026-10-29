#!/usr/bin/env bash
# Generates the WhatsApp PRODUCTION env block into a LOCAL file (mode 0600) and prints NOTHING on success.
# Run it on a trusted machine. You then open the file yourself, paste the lines into .env.production, re-encrypt
# (scripts/env.sh encrypt production) and delete the file. The values never appear in a terminal, a shell history or a chat.
#
#   scripts/generate_whatsapp_production_secrets.sh [output-file]   # default: ./.whatsapp-production-secrets.env
#
# Refuses to overwrite an existing file. The default name is git-ignored. Every run draws fresh random values: never copy the
# staging secrets into production. Secrets are hex (URL- and shell-safe) and meet the sidecar's rules: token/secret >= 32
# chars, master key >= 64 hex chars, DB password alphanumeric >= 24 chars.
#
# WHATSAPP_ENABLED is written as `false` on purpose: the first production deploy brings the sidecar up dark. Flip it to true
# only after the post-deploy smoke has passed (docs/sop/whatsapp_sidecar_production_deploy.md).
#
# BACK UP WHATSAPP_AUTH_MASTER_KEY in your password manager BEFORE deploying: losing or changing it makes every stored
# WhatsApp credential undecryptable and every linked number must re-pair.
set -euo pipefail
umask 077

out="${1:-.whatsapp-production-secrets.env}"
if [ -e "$out" ]; then
  echo "refusing to overwrite existing file: $out" >&2
  exit 1
fi
command -v openssl >/dev/null 2>&1 || { echo "openssl not found" >&2; exit 1; }

tmp="$(mktemp "${out}.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

{
  echo "# PRODUCTION. Generated $(date -u +%Y-%m-%dT%H:%M:%SZ). Paste into .env.production, re-encrypt, then delete this file."
  echo "# Back up WHATSAPP_AUTH_MASTER_KEY in your password manager first."
  echo "# Backend + sidecar (shared):"
  echo "WHATSAPP_ENABLED=false"
  echo "WHATSAPP_SIDECAR_URL=http://whatsapp-sidecar:8787"
  echo "WHATSAPP_MAX_CONNECTIONS=3"
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
