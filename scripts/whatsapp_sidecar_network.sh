#!/usr/bin/env bash
# Production only. Creates the private network the WhatsApp sidecar shares with exactly the three production containers it
# must reach, and attaches them. Idempotent: safe to run on every deploy and by hand. Prints names and status only.
#
#   luran_wa_backend   (bridge, NOT internal: the sidecar needs the internet for WhatsApp and Cloudinary)
#     luran_api     alias `api`   webhook target of the sidecar (http://api:8000), and the caller of the sidecar's HTTP API
#     luran_worker                the arq worker sends WhatsApp messages and runs the capacity cron through the sidecar's HTTP API
#     luran_db      alias `db`    the sidecar's direct Postgres connection (role whatsapp_sidecar, bypassing pgbouncer)
#
# Why a script and not docker-compose.prod.yml: declaring the network on api/worker/db there would recreate Postgres and
# change api/worker config. `docker network connect` attaches a RUNNING container without restarting it. The cost: a container
# that compose RECREATES comes back without the attachment, and the production deploy recreates api and worker every time, so
# cd.yml runs this script after every production deploy (and a manual `docker compose up -d` on the VPS needs it too).
#
# The sidecar is deliberately NOT on luran_default, so it cannot resolve or reach redis, qdrant or pgbouncer. To keep that true
# the script also FAILS if anything other than the four expected containers is on the network (a hand-run
# `docker network connect luran_wa_backend luran_redis` would otherwise undo the isolation without any DNS name changing).
#
#   whatsapp_sidecar_network.sh              create the network if missing, then attach (the sidecar deploy step)
#   whatsapp_sidecar_network.sh --if-exists  never create it; do nothing when it does not exist yet. Run after EVERY production
#                                            deploy, even when the sidecar gate skipped: compose just recreated api/worker.
set -euo pipefail

create=1
case "${1:-}" in
  "") ;;
  --if-exists) create=0 ;;
  *) echo "unknown argument: $1 (only --if-exists is supported)" >&2; exit 2 ;;
esac

NET="luran_wa_backend"
# container:alias (alias may be empty). Names are literal: this script only ever serves the production stack.
MEMBERS=("luran_api:api" "luran_worker:" "luran_db:db")

project="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' luran_api 2>/dev/null || true)"
if [ "$project" != "luran" ]; then
  echo "luran_api is not the production stack (compose project: ${project:-none}); refusing to touch networks" >&2
  exit 1
fi

if shape="$(docker network inspect --format '{{.Internal}} {{.Driver}}' "$NET" 2>/dev/null)"; then
  if [ "$shape" != "false bridge" ]; then
    echo "network $NET exists but is not a plain bridge with egress (found: ${shape}); refusing to use it" >&2
    exit 1
  fi
  echo "network $NET: exists"
  members="$(docker network inspect --format '{{range .Containers}}{{.Name}} {{end}}' "$NET" 2>/dev/null || true)"
  for name in $members; do
    case "$name" in
      luran_api|luran_worker|luran_db|luran_whatsapp_sidecar) ;;
      *) echo "container $name is on $NET but must not be; disconnect it (docker network disconnect $NET $name) - refusing to continue" >&2; exit 1 ;;
    esac
  done
elif [ "$create" -eq 0 ]; then
  echo "network $NET: does not exist yet, nothing to attach"
  exit 0
else
  docker network create --driver bridge --label luran.purpose=whatsapp-sidecar "$NET" >/dev/null
  echo "network $NET: created"
fi

for member in "${MEMBERS[@]}"; do
  container="${member%%:*}"
  alias="${member#*:}"
  if ! attached="$(docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' "$container" 2>/dev/null)"; then
    echo "container $container not found; cannot attach it to $NET" >&2
    exit 1
  fi
  if printf '%s\n' "$attached" | tr ' ' '\n' | grep -qxF "$NET"; then
    echo "$container: already on $NET"
    continue
  fi
  if [ -n "$alias" ]; then
    docker network connect --alias "$alias" "$NET" "$container"
  else
    docker network connect "$NET" "$container"
  fi
  echo "$container: attached to $NET"
done
