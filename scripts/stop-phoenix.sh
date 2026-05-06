#!/bin/sh
# stop-phoenix.sh — graceful shutdown of Phoenix backend at market close
#
# Called by cron at 00:00 IST (18:30 UTC) Sun-Thu to stop the backend.
# If the next trading morning (today IST, since we just crossed midnight) is an
# NSE holiday, nginx is also stopped for a complete system shutdown.
# Otherwise nginx stays up (serves 502 gracefully; LB health checks continue).
#
# Cron entry (on OCI VM, cron runs in UTC):
#   30 18 * * 0-4 /opt/phoenix/stop-phoenix.sh >> /opt/phoenix/logs/cron-scheduler.log 2>&1
#   (00:00 IST Mon-Fri = 18:30 UTC Sun-Thu)

set -eu

COMPOSE_FILE="/opt/phoenix/app/docker-compose.oci-live.yml"
OVERRIDE_FILE="/opt/phoenix/phoenix-override.yml"
ENV_FILE="/opt/phoenix/phoenix-deploy.env"
HOLIDAYS_FILE="/opt/phoenix/nse-holidays.txt"
LOG_TAG="stop-phoenix"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [$LOG_TAG] $*"; }

# At midnight IST the calendar day has just rolled — TODAY_IST is the next trading morning.
if command -v python3 >/dev/null 2>&1; then
    TODAY_IST=$(python3 -c "
from datetime import datetime, timezone, timedelta
ist = timezone(timedelta(hours=5, minutes=30))
print(datetime.now(ist).strftime('%Y-%m-%d'))
")
else
    TODAY_IST=$(date -u -d '+5 hours 30 minutes' '+%Y-%m-%d' 2>/dev/null \
                || date -u -v+5H -v+30M '+%Y-%m-%d')
fi

log "Stopping Phoenix backend (scheduled midnight IST shutdown) — next IST date: $TODAY_IST"

CONTROL_PLANE_PG_PASSWORD_HOST=dummy \
  docker compose \
    -f "$COMPOSE_FILE" \
    -f "$OVERRIDE_FILE" \
    --env-file "$ENV_FILE" \
    stop backend

log "Backend stopped."

# Check if the next trading morning (TODAY_IST) is an NSE holiday.
IS_HOLIDAY=0
if [ -f "$HOLIDAYS_FILE" ]; then
    while IFS= read -r line; do
        clean=$(echo "$line" | sed 's/#.*//' | tr -d ' \t\r')
        [ -z "$clean" ] && continue
        if [ "$clean" = "$TODAY_IST" ]; then
            IS_HOLIDAY=1
            break
        fi
    done < "$HOLIDAYS_FILE"
fi

if [ "$IS_HOLIDAY" = "1" ]; then
    log "NSE HOLIDAY ($TODAY_IST) — performing full shutdown (nginx + backend)."
    CONTROL_PLANE_PG_PASSWORD_HOST=dummy \
      docker compose \
        -f "$COMPOSE_FILE" \
        -f "$OVERRIDE_FILE" \
        --env-file "$ENV_FILE" \
        stop web
    log "Full shutdown complete. All Phoenix containers stopped for the holiday."
else
    log "Next trading day ($TODAY_IST) is a working day — nginx remains up."
fi

log "Container status:"
docker ps --filter name=phoenix-oci --format "  {{.Names}}: {{.Status}}"
