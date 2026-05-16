#!/bin/sh
# Fail-closed host precheck for the nightly optimizer systemd service.

set -eu

COMPOSE_FILE="${PHOENIX_COMPOSE_FILE:-/opt/phoenix/app/docker-compose.oci-live.yml}"
ENV_FILE="${PHOENIX_ENV_FILE:-/opt/phoenix/phoenix-deploy.env}"
STATE_DIR="${PHOENIX_STATE_HOST_PATH:-/opt/phoenix/state}"
OUTPUT_DIR="${PHOENIX_OPTIMIZER_OUTPUT_PATH:-/opt/phoenix/optimizer/output}"
LOCK_FILE="${OPTIMIZER_LOCK_FILE:-$STATE_DIR/optimizer.lock}"
LOG_TAG="optimizer-precheck"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [$LOG_TAG] $*"; }

fatal() {
    log "FATAL: $*"
    exit 1
}

ist_minute_of_day() {
    if command -v python3 >/dev/null 2>&1; then
        python3 - <<'PY'
from datetime import datetime, timezone, timedelta
ist = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(ist)
print(now.hour * 60 + now.minute)
PY
        return
    fi

    hour=$(TZ=Asia/Kolkata date '+%H' | sed 's/^0//')
    minute=$(TZ=Asia/Kolkata date '+%M' | sed 's/^0//')
    hour=${hour:-0}
    minute=${minute:-0}
    echo $((hour * 60 + minute))
}

command -v docker >/dev/null 2>&1 || fatal "docker CLI is not available."
[ -f "$COMPOSE_FILE" ] || fatal "compose file missing: $COMPOSE_FILE"
[ -f "$ENV_FILE" ] || fatal "deploy env file missing: $ENV_FILE"

mkdir -p "$STATE_DIR" "$OUTPUT_DIR"

# NSE continuous session guard. The optimizer is a post-market batch job and
# must never start while the live entry/exit path can be active.
IST_MINUTE=$(ist_minute_of_day)
MARKET_OPEN_MINUTE=$((9 * 60))
MARKET_CLOSE_GUARD_MINUTE=$((15 * 60 + 35))
if [ "$IST_MINUTE" -ge "$MARKET_OPEN_MINUTE" ] \
    && [ "$IST_MINUTE" -le "$MARKET_CLOSE_GUARD_MINUTE" ]; then
    fatal "optimizer blocked during NSE market-hours guard window 09:00-15:35 IST."
fi

if command -v flock >/dev/null 2>&1; then
    flock -n "$LOCK_FILE" true || fatal "optimizer lock is already held: $LOCK_FILE"
else
    LOCK_DIR="${LOCK_FILE}.d"
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        fatal "optimizer fallback lock is already held: $LOCK_DIR"
    fi
    rmdir "$LOCK_DIR"
fi

if docker ps --filter name=phoenix-oci-backend --filter status=running -q \
    | grep -q .; then
    if docker logs --since 5m phoenix-oci-backend 2>&1 \
        | grep -Eiq 'order_placed|ORDER_PLACED'; then
        fatal "recent order placement marker found in backend logs; optimizer blocked."
    fi
fi

log "passed"
