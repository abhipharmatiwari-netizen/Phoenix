#!/bin/sh
# start-phoenix.sh — start Phoenix backend on trading days at market pre-open
#
# Called by cron at 08:00 IST (02:30 UTC) Mon-Fri.
# Skips start on NSE market holidays listed in /opt/phoenix/nse-holidays.txt.
#
# Cron entry (on OCI VM, cron runs in UTC):
#   30 2 * * 1-5 /opt/phoenix/start-phoenix.sh >> /opt/phoenix/logs/cron-scheduler.log 2>&1
#   (08:00 IST Mon-Fri = 02:30 UTC Mon-Fri)
#
# Holiday file format: /opt/phoenix/nse-holidays.txt
#   One date per line in YYYY-MM-DD format (IST calendar date).
#   Lines starting with # are comments.
#   Update annually or when NSE announces special sessions/closures.

set -eu

COMPOSE_FILE="/opt/phoenix/app/docker-compose.oci-live.yml"
OVERRIDE_FILE="/opt/phoenix/phoenix-override.yml"
ENV_FILE="/opt/phoenix/phoenix-deploy.env"
HOLIDAYS_FILE="/opt/phoenix/nse-holidays.txt"
LOG_TAG="start-phoenix"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [$LOG_TAG] $*"; }

# Current IST date (UTC + 5:30 = UTC + 5h30m).
# We use Python for reliable IST date calculation; falls back to date if unavailable.
if command -v python3 >/dev/null 2>&1; then
    TODAY_IST=$(python3 -c "
from datetime import datetime, timezone, timedelta
ist = timezone(timedelta(hours=5, minutes=30))
print(datetime.now(ist).strftime('%Y-%m-%d'))
")
else
    # Fallback: add 5.5 hours to UTC. Imprecise near midnight but cron fires at 08:00 IST.
    TODAY_IST=$(date -u -d '+5 hours 30 minutes' '+%Y-%m-%d' 2>/dev/null \
                || date -u -v+5H -v+30M '+%Y-%m-%d')
fi

log "Today (IST): $TODAY_IST"

# Check if today is an NSE holiday.
if [ -f "$HOLIDAYS_FILE" ]; then
    while IFS= read -r line; do
        # Strip comments and whitespace.
        clean=$(echo "$line" | sed 's/#.*//' | tr -d ' \t\r')
        [ -z "$clean" ] && continue
        if [ "$clean" = "$TODAY_IST" ]; then
            log "NSE HOLIDAY ($TODAY_IST) — Phoenix will not start today."
            exit 0
        fi
    done < "$HOLIDAYS_FILE"
fi

# Check backend is not already running.
if docker ps --filter name=phoenix-oci-backend --filter status=running -q | grep -q .; then
    log "Backend already running — no action needed."
    docker ps --filter name=phoenix-oci --format "  {{.Names}}: {{.Status}}"
    exit 0
fi

log "Starting Phoenix backend for trading day $TODAY_IST"

# Git pull to pick up any overnight code pushes.
if [ -d /opt/phoenix/app/.git ]; then
    git -C /opt/phoenix/app config --global --add safe.directory /opt/phoenix/app 2>/dev/null || true
    git -C /opt/phoenix/app pull origin main 2>&1 | tail -3 || log "WARNING: git pull failed — continuing with existing code"
fi

CONTROL_PLANE_PG_PASSWORD_HOST=dummy \
  docker compose \
    -f "$COMPOSE_FILE" \
    -f "$OVERRIDE_FILE" \
    --env-file "$ENV_FILE" \
    up -d --no-deps backend

log "Backend started. Waiting 90s for healthcheck..."
sleep 90

STATUS=$(docker ps --filter name=phoenix-oci-backend --format "{{.Status}}" 2>/dev/null || echo "unknown")
log "Backend status: $STATUS"

if echo "$STATUS" | grep -q "healthy"; then
    log "Phoenix is healthy and ready for trading."
else
    log "WARNING: Backend may not be healthy yet — check logs: docker logs phoenix-oci-backend --tail 20"
fi
