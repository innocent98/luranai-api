#!/usr/bin/env bash
# Production-only env preparation, run by cd.yml's production-deploy job on the RUNNER after the encrypted production env
# is decrypted to ./.env and before it is copied to the VPS. The mirror of scripts/prepare_staging_whatsapp_env.sh with a
# PRODUCTION guard instead of the staging project check, and with no api memory floor (production's api limit is not ours).
#
#   1. If the env carries none of the sidecar names, does NOTHING (no file is rewritten, no mode changed): the merge of the
#      code that contains this script is a no-op for production until the owner adds the secrets.
#   2. Otherwise decides whether the WhatsApp sidecar can be deployed (every required NAME present and well-formed, and the
#      env really is the production stack). If not, the sidecar is skipped with a log line and the deploy continues.
#   3. Moves the sidecar-only secrets (master key, DB password) out of .env into .env.sidecar so the api/worker
#      env_file never carries them. This happens whether or not the gate passes.
#
# Prints variable NAMES and pass/fail only. Never prints, logs or echoes a value.
# Output: appends `sidecar_enabled=true|false` and, when true, `backend_mode=enabled|disabled` (is WHATSAPP_ENABLED on in the
# backend env; drives which way the post-deploy smoke judges the webhook) to $GITHUB_OUTPUT when set.
# This script never sets or changes WHATSAPP_ENABLED.
set -euo pipefail
umask 077

ENV_FILE="${1:-.env}"
SIDECAR_ENV_FILE="${2:-.env.sidecar}"
# Not overridable: this script only ever serves the production stack.
EXPECT_PROJECT="luran"
EXPECT_ENVIRONMENT="production"

# Names that must never reach the api/worker container env.
SIDECAR_ONLY_KEYS=(WHATSAPP_AUTH_MASTER_KEY WHATSAPP_SIDECAR_DB_PASSWORD)
# Names the sidecar needs; all must be present and well-formed to deploy it.
REQUIRED_KEYS=(WHATSAPP_SIDECAR_TOKEN WHATSAPP_WEBHOOK_SECRET WHATSAPP_AUTH_MASTER_KEY WHATSAPP_SIDECAR_DB_PASSWORD)
# Names the backend needs to talk to it; missing ones only warn.
BACKEND_KEYS=(WHATSAPP_ENABLED WHATSAPP_SIDECAR_URL WHATSAPP_CONTACT_PEPPER)
# Sidecar-side session cap when WHATSAPP_MAX_SESSIONS is not set (docker-compose.whatsapp.production.yml) and the backend's
# own default for WHATSAPP_MAX_CONNECTIONS (app/core/config.py).
DEFAULT_SIDECAR_MAX_SESSIONS=3
DEFAULT_BACKEND_MAX_CONNECTIONS=3

emit() { [ -z "${GITHUB_OUTPUT:-}" ] || echo "$1" >> "$GITHUB_OUTPUT"; }
summary() { [ -z "${GITHUB_STEP_SUMMARY:-}" ] || echo "$1" >> "$GITHUB_STEP_SUMMARY"; }
skip() {
  echo "WhatsApp sidecar: SKIPPED - $1"
  summary "**WhatsApp sidecar skipped:** $1"
  emit "sidecar_enabled=false"
}

# Value of KEY (last assignment wins, matching surrounding quotes stripped). Callers must not print it.
# Accepts what compose's dotenv parser accepts: leading whitespace, `export`, spaces around `=`.
# Exit status 1 when KEY is not assigned at all (an empty assignment still succeeds).
val() {
  awk -v k="$1" '
    { sub(/\r$/, ""); line = $0; sub(/^[ \t]*(export[ \t]+)?/, "", line) }
    index(line, k) == 1 {
      rest = substr(line, length(k) + 1)
      if (rest ~ /^[ \t]*=/) { sub(/^[ \t]*=[ \t]*/, "", rest); v = rest; found = 1 }
    }
    END {
      if (!found) exit 1
      if (v ~ /^".*"$/ || v ~ /^'"'"'.*'"'"'$/) v = substr(v, 2, length(v) - 2)
      print v
    }' "$ENV_FILE"
}
present() { val "$1" >/dev/null 2>&1; }
has() { local v; v="$(val "$1" 2>/dev/null || true)"; [ -n "$v" ]; }
truthy() { local v; v="$(val "$1" 2>/dev/null | tr '[:upper:]' '[:lower:]' || true)"; case "$v" in true|1|yes|on|t|y) return 0 ;; *) return 1 ;; esac; }
num_or() { local v; v="$(val "$1" 2>/dev/null || true)"; if printf '%s' "$v" | grep -Eq '^[0-9]+$'; then printf '%s' "$v"; else printf '%s' "$2"; fi; }

# Rewrite $ENV_FILE without any assignment of the given keys, appending the removed lines to $1.
strip_keys() {
  local out="$1" keys_re
  shift
  keys_re="$(IFS='|'; echo "$*")"
  awk -v re="^[ \\t]*(export[ \\t]+)?(${keys_re})[ \\t]*=" -v moved="$out" '
    $0 ~ re { if (moved != "") print >> moved; next }
    { print }' "$ENV_FILE" > "${ENV_FILE}.tmp"
  mv "${ENV_FILE}.tmp" "$ENV_FILE"
}

if [ ! -f "$ENV_FILE" ]; then
  skip "no decrypted env on the runner (the server's existing .env is used); cannot evaluate the required names"
  exit 0
fi

# --- 1. nothing to do: the owner has not added any sidecar name -----------------------------------------------------
any_name=0
for k in "${REQUIRED_KEYS[@]}" "${SIDECAR_ONLY_KEYS[@]}"; do present "$k" && any_name=1; done
if [ "$any_name" -eq 0 ]; then
  if truthy WHATSAPP_ENABLED; then
    echo "::warning::WHATSAPP_ENABLED is on in the production env but no sidecar names exist: pairing cannot work until the sidecar is set up"
  fi
  skip "no WhatsApp sidecar names in the production env (WhatsApp ships dark); env left untouched"
  exit 0
fi

# --- 2. gate: evaluate BEFORE the sidecar-only keys are moved out ---------------------------------------------------
problems=()
missing=()
for k in "${REQUIRED_KEYS[@]}"; do has "$k" || missing+=("$k"); done
[ "${#missing[@]}" -eq 0 ] || problems+=("missing or empty: ${missing[*]}")

if [ "${#missing[@]}" -eq 0 ]; then
  bad=()
  [ "$(val WHATSAPP_SIDECAR_TOKEN | wc -c)" -ge 33 ] || bad+=(WHATSAPP_SIDECAR_TOKEN)
  [ "$(val WHATSAPP_WEBHOOK_SECRET | wc -c)" -ge 33 ] || bad+=(WHATSAPP_WEBHOOK_SECRET)
  val WHATSAPP_AUTH_MASTER_KEY | grep -Eq '^([0-9a-fA-F]{2}){32,}$' || bad+=(WHATSAPP_AUTH_MASTER_KEY)
  val WHATSAPP_SIDECAR_DB_PASSWORD | grep -Eq '^[A-Za-z0-9]{24,}$' || bad+=(WHATSAPP_SIDECAR_DB_PASSWORD)
  [ "${#bad[@]}" -eq 0 ] || problems+=("malformed (length/charset rules in docs/sop/whatsapp_sidecar_production_deploy.md): ${bad[*]}")
fi

# Production-isolation guard (the mirror of staging's `COMPOSE_PROJECT_NAME is luran_staging`): the env must be the
# production stack's. The project name, when the env sets one, must be exactly `luran` (an unset name resolves to `luran`
# from the stack directory; the deploy step re-checks the running api container's compose label on the server), and
# ENVIRONMENT, when set, must be `production`. A staging env pasted into the production file is therefore refused.
if present COMPOSE_PROJECT_NAME; then
  project="$(val COMPOSE_PROJECT_NAME 2>/dev/null || true)"
  [ "$project" = "$EXPECT_PROJECT" ] || problems+=("COMPOSE_PROJECT_NAME is not ${EXPECT_PROJECT}")
fi
if present ENVIRONMENT; then
  environment="$(val ENVIRONMENT 2>/dev/null || true)"
  [ "$environment" = "$EXPECT_ENVIRONMENT" ] || problems+=("ENVIRONMENT is not ${EXPECT_ENVIRONMENT}")
fi

backend_missing=()
for k in "${BACKEND_KEYS[@]}"; do has "$k" || backend_missing+=("$k"); done

# --- 3. keep sidecar-only secrets out of the api/worker env_file ----------------------------------------------------
: > "$SIDECAR_ENV_FILE"
strip_keys "$SIDECAR_ENV_FILE" "${SIDECAR_ONLY_KEYS[@]}"
# 644, not 600: these are runner-local, ephemeral files and the scp-action that ships them runs in a container as a
# different user, which cannot read a 600 file ("tar: can't open '.env': Permission denied", luranai-api #165). The sidecar
# deploy step tightens .env.sidecar to 600 on the server as soon as it arrives.
chmod 644 "$ENV_FILE" "$SIDECAR_ENV_FILE"
echo "Sidecar-only secrets (names: ${SIDECAR_ONLY_KEYS[*]}) kept out of the api/worker env file"

if [ "${#problems[@]}" -gt 0 ]; then
  rm -f "$SIDECAR_ENV_FILE"
  skip "$(IFS=';'; echo "${problems[*]}")"
  exit 0
fi

[ "${#backend_missing[@]}" -eq 0 ] || echo "::warning::WhatsApp sidecar will deploy but the backend env lacks: ${backend_missing[*]} (pairing will not work until they are set)"

# The backend's cap on connections must not exceed what the sidecar admits, or tenants get a 503 from the sidecar instead of the
# clean plan-limit message from the backend.
backend_cap="$(num_or WHATSAPP_MAX_CONNECTIONS "$DEFAULT_BACKEND_MAX_CONNECTIONS")"
sidecar_cap="$(num_or WHATSAPP_MAX_SESSIONS "$DEFAULT_SIDECAR_MAX_SESSIONS")"
if [ "$backend_cap" -gt "$sidecar_cap" ]; then
  echo "::warning::WHATSAPP_MAX_CONNECTIONS (backend) is higher than the sidecar's session cap; lower it or raise WHATSAPP_MAX_SESSIONS after a soak"
fi

if truthy WHATSAPP_ENABLED; then mode=enabled; else mode=disabled; fi
echo "WhatsApp sidecar: ENABLED for deploy - all required names present and well-formed; backend WHATSAPP_ENABLED is ${mode}"
summary "**WhatsApp sidecar enabled** for this production deploy (backend WhatsApp: ${mode})."
emit "sidecar_enabled=true"
emit "backend_mode=${mode}"
