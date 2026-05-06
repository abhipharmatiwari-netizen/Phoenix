#!/bin/sh
# stop-phoenix.sh — graceful shutdown of Phoenix backend at market close
#
# Called by cron at 00:00 IST (18:30 UTC) Sun-Thu to stop the backend.
# nginx stays up (serves 502 gracefully if hit; LB health check on /health
# which proxies to backend — will fail but that is acceptable overnight).
#
# Cron entry (on OCI VM, cron runs in UTC):
#   30 18 * * 0-4 /opt/phoenix/stop-phoenix.sh >> /opt/phoenix/logs/cron-scheduler.log 2>&1
#   (00:00 IST Mon-Fri = 18:30 UTC Sun-Thu)

set -eu

COMPOSE_FILE="/opt/phoenix/app/docker-compose.oci-live.yml"
OVERRIDE_FILE="/opt/phoenix/phoenix-override.yml"
ENV_FILE="/opt/phoenix/phoenix-deploy.env"
LOG_TAG="stop-phoenix"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [$LOG_TAG] $*"; }

log "Stopping Phoenix backend (scheduled midnight IST shutdown)"

CONTROL_PLANE_PG_PASSWORD_HOST=dummy \
  docker compose \
    -f "$COMPOSE_FILE" \
    -f "$OVERRIDE_FILE" \
    --env-file "$ENV_FILE" \
    stop backend

log "Backend stopped. nginx remains up."
log "Container status:"
docker ps --filter name=phoenix-oci --format "  {{.Names}}: {{.Status}}"
