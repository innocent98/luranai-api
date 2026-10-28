#!/usr/bin/env bash
# Staging-only env preparation, run by cd.yml's staging-deploy job on the RUNNER after the encrypted staging env is
# decrypted to ./.env and before it is copied to the VPS. Never run for production.
#
#   1. Decides whether the WhatsApp sidecar can be deployed (all required NAMES present with usable values). If not, the
#      sidecar is skipped with a log line and the rest of the staging deploy continues.
#   2. Moves the sidecar-only secrets (master key, DB password) out of .env into .env.sidecar so the api/worker
#      env_file never carries them.
#   3. Raises API_MEM_LIMIT to a floor (the staging api sat at 448 of 500 MiB). Never lowers an existing higher value.
#
# Prints variable NAMES and pass/fail only. Never prints, logs or echoes a value.
# Output: appends `sidecar_enabled=true|false` to $GITHUB_OUTPUT when set.
set -euo pipefail
umask 077

ENV_FILE="${1:-.env}"
SIDECAR_ENV_FILE="${2:-.env.sidecar}"
EXPECT_PROJECT="${EXPECT_COMPOSE_PROJECT:-luran_staging}"
API_MEM_FLOOR_MIB="${STAGING_API_MEM_FLOOR_MIB:-768}"

# Names that must never reach the api/worker container env.
SIDECAR_ONLY_KEYS=(WHATSAPP_AUTH_MASTER_KEY WHATSAPP_SIDECAR_DB_PASSWORD)
# Names the sidecar needs; all must be present and well-formed to deploy it.
REQUIRED_KEYS=(WHATSAPP_SIDECAR_TOKEN WHATSAPP_WEBHOOK_SECRET WHATSAPP_AUTH_MASTER_KEY WHATSAPP_SIDECAR_DB_PASSWORD)
# Names the backend needs to talk to it; missing ones only warn.
BACKEND_KEYS=(WHATSAPP_ENABLED WHATSAPP_SIDECAR_URL WHATSAPP_CONTACT_PEPPER)

emit() { [ -z "${GITHUB_OUTPUT:-}" ] || echo "$1" >> "$GITHUB_OUTPUT"; }
summary() { [ -z "${GITHUB_STEP_SUMMARY:-}" ] || echo "$1" >> "$GITHUB_STEP_SUMMARY"; }
skip() {
  echo "WhatsApp sidecar: SKIPPED - $1"
  summary "**WhatsApp sidecar skipped:** $1"
  emit "sidecar_enabled=false"
}

# Value of KEY (last assignment wins, matching surrounding quotes stripped). Callers must not print it.
# Accepts what compose's dotenv parser accepts: leading whitespace, `export`, spaces around `=`.
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
has() { local v; v="$(val "$1" 2>/dev/null || true)"; [ -n "$v" ]; }

# Rewrite $ENV_FILE without any assignment of the given keys, writing the removed lines to $2 (appended) when set.
strip_keys() {
  local out="$1" keys_re
  shift
  keys_re="$(IFS='|'; echo "$*")"
  awk -v re="^[ \\t]*(export[ \\t]+)?(${keys_re})[ \\t]*=" -v moved="$out" '
    $0 ~ re { if (moved != "") print >> moved; next }
    { print }' "$ENV_FILE" > "${ENV_FILE}.tmp"
  mv "${ENV_FILE}.tmp" "$ENV_FILE"
}

mem_to_mib() {   # Docker size ("500m", "1g", "1.5GiB", "2048k", "524288000") -> whole MiB; empty if unparsable
  printf '%s' "$1" | awk '
    { s = tolower($0) }
    s !~ /^[0-9]+(\.[0-9]+)?(b|k|kb|kib|m|mb|mib|g|gb|gib)?$/ { exit }
    {
      n = s + 0; u = s; sub(/^[0-9.]+/, "", u)
      f = 1 / 1048576
      if (u ~ /^k/) f = 1 / 1024; else if (u ~ /^m/) f = 1; else if (u ~ /^g/) f = 1024
      printf "%d\n", n * f
    }'
}

if [ ! -f "$ENV_FILE" ]; then
  skip "no decrypted env on the runner (the server's existing .env is used); cannot evaluate the required names"
  exit 0
fi

# --- 3. staging api memory floor (independent of the sidecar gate) -------------------------------------------------
current_mib=""
current_raw=""
if current_raw="$(val API_MEM_LIMIT 2>/dev/null)"; then current_mib="$(mem_to_mib "$current_raw")"; fi
if [ -n "$current_raw" ] && [ -z "$current_mib" ]; then
  echo "::warning::API_MEM_LIMIT has a value this script cannot parse; left unchanged (set it to at least ${API_MEM_FLOOR_MIB}m yourself)"
elif [ -z "$current_mib" ] || [ "$current_mib" -lt "$API_MEM_FLOOR_MIB" ]; then
  awk '!/^[ \t]*(export[ \t]+)?API_MEM_LIMIT[ \t]*=/' "$ENV_FILE" > "${ENV_FILE}.tmp"
  printf 'API_MEM_LIMIT=%sm\n' "$API_MEM_FLOOR_MIB" >> "${ENV_FILE}.tmp"
  mv "${ENV_FILE}.tmp" "$ENV_FILE"
  echo "Staging API_MEM_LIMIT raised to ${API_MEM_FLOOR_MIB}m (was below the floor or unset)"
else
  echo "Staging API_MEM_LIMIT already at or above ${API_MEM_FLOOR_MIB}m; unchanged"
fi

# --- gate: evaluate BEFORE the sidecar-only keys are moved out ------------------------------------------------------
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
  [ "${#bad[@]}" -eq 0 ] || problems+=("malformed (length/charset rules in docs/sop/whatsapp_sidecar_staging_deploy.md): ${bad[*]}")
fi

# Production-isolation guard: the sidecar container and network names derive from COMPOSE_PROJECT_NAME.
project="$(val COMPOSE_PROJECT_NAME 2>/dev/null || true)"
[ "$project" = "$EXPECT_PROJECT" ] || problems+=("COMPOSE_PROJECT_NAME is not ${EXPECT_PROJECT}")

backend_missing=()
for k in "${BACKEND_KEYS[@]}"; do has "$k" || backend_missing+=("$k"); done

# --- 2. keep sidecar-only secrets out of the api/worker env_file ----------------------------------------------------
: > "$SIDECAR_ENV_FILE"
strip_keys "$SIDECAR_ENV_FILE" "${SIDECAR_ONLY_KEYS[@]}"
chmod 600 "$ENV_FILE" "$SIDECAR_ENV_FILE"
echo "Sidecar-only secrets (names: ${SIDECAR_ONLY_KEYS[*]}) kept out of the api/worker env file"

if [ "${#problems[@]}" -gt 0 ]; then
  rm -f "$SIDECAR_ENV_FILE"
  skip "$(IFS=';'; echo "${problems[*]}")"
  exit 0
fi

[ "${#backend_missing[@]}" -eq 0 ] || echo "::warning::WhatsApp sidecar will deploy but the backend env lacks: ${backend_missing[*]} (pairing will not work until they are set)"
echo "WhatsApp sidecar: ENABLED - all required names present and well-formed"
summary "**WhatsApp sidecar enabled** for this staging deploy."
emit "sidecar_enabled=true"
