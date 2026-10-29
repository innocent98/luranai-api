#!/usr/bin/env bash
# One-line health snapshot of the production WhatsApp sidecar, meant to run from a USER crontab every 5 minutes (no sudo).
# Appends ONE CSV line to a log file and prints nothing on success:
#
#   utc,mem_current_b,anon_b,sessions,rss_mb,socket_closed_5m,err
#   2026-10-29T14:05:01Z,93323264,71827456,2,139.4,0,
#
#   mem_current_b     container cgroup memory.current (what the OOM killer judges; includes page cache and /tmp tmpfs)
#   anon_b            cgroup anon memory (the real process heap)
#   sessions, rss_mb  from the sidecar's own GET /health (live session count, process RSS in MB)
#   socket_closed_5m  number of `socket closed` log lines in the last 5 minutes (Baileys reconnects; staging saw 13 in 17 h)
#   err               empty when everything was read, else a short code: container_down, exec_failed, cgroup_unreadable,
#                     health_<http status>, health_unparsable
#
# Memory and /health are read from INSIDE the container with one `docker exec` that runs bash and cat only, so the bearer
# token is expanded by the container's own shell: it never reaches this script, its arguments, the log or the cron mail.
# Nothing here prints a secret, a phone number or a session key. One line is about 60 bytes: 288 lines/day is ~0.5 MB/month.
#
#   scripts/whatsapp_sidecar_snapshot.sh [container-name]        # default container: luran_whatsapp_sidecar
#
# Environment (all optional):
#   WA_SNAPSHOT_LOG        log file       (default: <repo dir>/logs/whatsapp_sidecar_snapshot.csv, next to the app logs)
#   WA_SNAPSHOT_MAX_BYTES  rotate above   (default: 262144; the file is moved to <log>.1, <log>.1 to <log>.2, ...)
#   WA_SNAPSHOT_KEEP       rotated files kept (default: 2). Worst case disk use is (KEEP + 1) x MAX_BYTES = under 1 MB.
#   WA_SNAPSHOT_SINCE      log window for the `socket closed` count (default: 5m; keep it equal to the cron interval)
#   WA_SNAPSHOT_DOCKER     docker binary  (default: docker; a test hook)
#
# Exit status: 0 whenever a line was written (even one carrying an err code, so cron does not mail every 5 minutes while the
# sidecar is down: the err column is the signal); 1 only when the log cannot be written, with the reason on stderr.
set -euo pipefail
umask 077

container="${1:-${WA_SNAPSHOT_CONTAINER:-luran_whatsapp_sidecar}}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log="${WA_SNAPSHOT_LOG:-${script_dir}/../logs/whatsapp_sidecar_snapshot.csv}"
max_bytes="${WA_SNAPSHOT_MAX_BYTES:-262144}"
keep="${WA_SNAPSHOT_KEEP:-2}"
since="${WA_SNAPSHOT_SINCE:-5m}"
docker_bin="${WA_SNAPSHOT_DOCKER:-docker}"
header="utc,mem_current_b,anon_b,sessions,rss_mb,socket_closed_5m,err"

case "$max_bytes$keep" in *[!0-9]*) echo "WA_SNAPSHOT_MAX_BYTES and WA_SNAPSHOT_KEEP must be whole numbers" >&2; exit 1 ;; esac
case "$container" in *[!A-Za-z0-9_.-]*|"") echo "invalid container name" >&2; exit 1 ;; esac

# Bound every docker call so a hung daemon cannot pile up cron jobs. `timeout` is coreutils (the VPS); absent elsewhere.
bounded() { if command -v timeout >/dev/null 2>&1; then timeout 25 "$@"; else "$@"; fi; }

# Runs inside the container (bash + cat + grep + sed only). The token is expanded by the container's shell, never here.
read -r -d '' probe <<'PROBE' || true
cg=/sys/fs/cgroup
v=$(cat $cg/memory.current 2>/dev/null) && echo "cur $v"
v=$(grep -E "^anon " $cg/memory.stat 2>/dev/null | sed "s/^anon //") && [ -n "$v" ] && echo "anon $v"
exec 3<>"/dev/tcp/127.0.0.1/${PORT:-8787}" || exit 0
printf "GET /health HTTP/1.0\r\nHost: sidecar\r\nAuthorization: Bearer %s\r\nConnection: close\r\n\r\n" "$WHATSAPP_SIDECAR_TOKEN" >&3
if command -v timeout >/dev/null 2>&1; then resp=$(timeout 10 cat <&3); else resp=$(cat <&3); fi
code=${resp:9:3}
body=${resp#*$'\r\n\r\n'}
echo "http $code ${body//$'\n'/ }"
PROBE

mem="" anon="" sessions="" rss="" err=""
if out="$(bounded "$docker_bin" exec "$container" bash -c "$probe" 2>/dev/null)"; then
  while IFS= read -r line; do
    case "$line" in
      "cur "*) mem="${line#cur }" ;;
      "anon "*) anon="${line#anon }" ;;
      "http "*)
        code="${line#http }"; code="${code%% *}"
        body="${line#http "$code"}"
        if [ "$code" = 200 ]; then
          sessions="$(printf '%s' "$body" | sed -n 's/.*"sessions":[[:space:]]*\([0-9][0-9]*\).*/\1/p')"
          rss="$(printf '%s' "$body" | sed -n 's/.*"rss_mb":[[:space:]]*\([0-9][0-9]*\(\.[0-9]*\)\{0,1\}\).*/\1/p')"
          [ -n "$sessions" ] && [ -n "$rss" ] || err="health_unparsable"
        else
          err="health_${code}"
        fi
        ;;
    esac
  done <<< "$out"
  case "$mem$anon" in *[!0-9]*) mem=""; anon="" ;; esac
  if [ -z "$err" ] && { [ -z "$mem" ] || [ -z "$anon" ]; }; then err="cgroup_unreadable"; fi
  if [ -z "$err" ] && [ -z "$sessions" ]; then err="exec_failed"; fi
else
  if [ "$(bounded "$docker_bin" inspect --format '{{.State.Running}}' "$container" 2>/dev/null || echo unknown)" = true ]; then
    err="exec_failed"
  else
    err="container_down"
  fi
fi

closed="$({ bounded "$docker_bin" logs --since "$since" "$container" 2>&1 || true; } | grep -c 'socket closed' || true)"
case "$closed" in ""|*[!0-9]*) closed=0 ;; esac

mkdir -p "$(dirname "$log")" 2>/dev/null || true
if [ -e "$log" ]; then
  [ -w "$log" ] || writable=no
else
  [ -w "$(dirname "$log")" ] || writable=no
fi
if [ "${writable:-yes}" = no ]; then
  echo "cannot write ${log} (check that the directory and file belong to the user running this)" >&2
  exit 1
fi

if [ -f "$log" ] && [ "$(wc -c < "$log")" -ge "$max_bytes" ]; then
  if [ "$keep" -ge 1 ]; then
    rm -f "${log}.${keep}"
    for ((i = keep - 1; i >= 1; i--)); do
      [ -f "${log}.${i}" ] && mv -f "${log}.${i}" "${log}.$((i + 1))"
    done
    mv -f "$log" "${log}.1"
  else
    rm -f "$log"
  fi
fi
[ -s "$log" ] || echo "$header" > "$log"
printf '%s,%s,%s,%s,%s,%s,%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$mem" "$anon" "$sessions" "$rss" "$closed" "$err" >> "$log"
