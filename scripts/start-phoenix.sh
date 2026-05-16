#!/bin/sh
# start-phoenix.sh — start Phoenix backend on trading days at market pre-open
#
# Called by cron at 09:00 IST (03:30 UTC) Mon-Fri.
# Skips start on NSE market holidays listed in /opt/phoenix/nse-holidays.txt.
# Restarts nginx first if it was stopped during a prior holiday full-shutdown.
#
# Cron entry (on OCI VM, cron runs in UTC):
#   30 3 * * 1-5 /opt/phoenix/start-phoenix.sh >> /opt/phoenix/logs/cron-scheduler.log 2>&1
#   (09:00 IST Mon-Fri = 03:30 UTC Mon-Fri)
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

read_env_value() {
    key="$1"
    [ -f "$ENV_FILE" ] || return 1
    sed -n "s/^[[:space:]]*$key[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" \
        | tail -n 1 \
        | sed "s/[[:space:]]#.*$//; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^['\"]//; s/['\"]$//"
}

resolve_image_refs() {
    BACKEND_IMAGE=$(read_env_value PHOENIX_BACKEND_IMAGE || true)
    NGINX_IMAGE=$(read_env_value PHOENIX_NGINX_IMAGE || true)

    if [ -n "$BACKEND_IMAGE" ] && [ -n "$NGINX_IMAGE" ]; then
        return
    fi

    case "$IMAGE_TAG" in
        local-*)
            BACKEND_IMAGE="${BACKEND_IMAGE:-phoenix-local-backend:${IMAGE_TAG}}"
            NGINX_IMAGE="${NGINX_IMAGE:-phoenix-local-nginx:${IMAGE_TAG}}"
            ;;
        *)
            OCIR_NAMESPACE=$(read_env_value OCIR_NAMESPACE || true)
            OCIR_REGION=$(read_env_value OCIR_REGION || true)
            OCIR_REGION="${OCIR_REGION:-ap-mumbai-1}"
            if [ -z "${OCIR_NAMESPACE:-}" ] || [ "$OCIR_NAMESPACE" = "CHANGE_ME" ]; then
                log "FATAL: OCIR_NAMESPACE not set in $ENV_FILE and IMAGE_TAG is not local-*."
                log "       Set OCIR_NAMESPACE for OCIR images or PHOENIX_BACKEND_IMAGE/PHOENIX_NGINX_IMAGE for an explicit image pair."
                exit 1
            fi
            IMAGE_BASE="${OCIR_REGION}.ocir.io/${OCIR_NAMESPACE}/phoenix-prod"
            BACKEND_IMAGE="${BACKEND_IMAGE:-${IMAGE_BASE}/backend:${IMAGE_TAG}}"
            NGINX_IMAGE="${NGINX_IMAGE:-${IMAGE_BASE}/nginx:${IMAGE_TAG}}"
            ;;
    esac
}

require_local_image() {
    service="$1"
    image="$2"
    if ! docker image inspect "$image" >/dev/null 2>&1; then
        log "FATAL: $service image '$image' not present locally."
        log "       Backend and nginx images must both be built or pulled for IMAGE_TAG=$IMAGE_TAG before scheduled start."
        log "       OCIR flow: run scripts/ops/build_push_ip.sh or build_and_push_image.sh, then scripts/ops/redeploy_backend.sh."
        log "       Local flow: docker build -t phoenix-local-backend:${IMAGE_TAG} -f Dockerfile ."
        log "                   docker build -t phoenix-local-nginx:${IMAGE_TAG} -f nginx/Dockerfile ."
        exit 1
    fi
}

# Current IST date (UTC + 5:30).
# Python used for reliable IST date calculation; falls back to date if unavailable.
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

log "Today (IST): $TODAY_IST"

# Check if today is an NSE holiday.
if [ -f "$HOLIDAYS_FILE" ]; then
    while IFS= read -r line; do
        clean=$(echo "$line" | sed 's/#.*//' | tr -d ' \t\r')
        [ -z "$clean" ] && continue
        if [ "$clean" = "$TODAY_IST" ]; then
            log "NSE HOLIDAY ($TODAY_IST) — Phoenix will not start today."
            exit 0
        fi
    done < "$HOLIDAYS_FILE"
fi

# Validate pinned images before any compose start path.
IMAGE_TAG=$(read_env_value IMAGE_TAG || true)
if [ -z "${IMAGE_TAG:-}" ] || [ "$IMAGE_TAG" = "CHANGE_ME" ]; then
    log "FATAL: IMAGE_TAG not set to a deployable value in $ENV_FILE."
    exit 1
fi
resolve_image_refs
require_local_image "backend" "$BACKEND_IMAGE"
require_local_image "nginx" "$NGINX_IMAGE"

# Ensure nginx is running; it may have been stopped during a holiday full-shutdown.
if ! docker ps --filter name=phoenix-oci-web --filter status=running -q | grep -q .; then
    log "nginx not running — starting it before backend (post-holiday recovery)."
    CONTROL_PLANE_PG_PASSWORD_HOST=dummy \
      docker compose \
        -f "$COMPOSE_FILE" \
        -f "$OVERRIDE_FILE" \
        --env-file "$ENV_FILE" \
        up -d --no-deps nginx
    sleep 5
fi

# Check backend is not already running.
if docker ps --filter name=phoenix-oci-backend --filter status=running -q | grep -q .; then
    log "Backend already running — no action needed."
    docker ps --filter name=phoenix-oci --format "  {{.Names}}: {{.Status}}"
    exit 0
fi

log "Starting Phoenix backend for trading day $TODAY_IST"

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
