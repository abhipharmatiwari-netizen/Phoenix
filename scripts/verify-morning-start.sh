#!/bin/sh
# verify-morning-start.sh — verify the 09:00 IST start succeeded; self-heal once.
#
# Runs at 09:10 IST (03:40 UTC) Mon-Fri via phoenix-verify.timer.
# Exits 0 if today is an NSE holiday OR backend is healthy.
# Otherwise invokes start-phoenix.sh once and re-checks. If still unhealthy,
# emits a prominent FATAL line that operators can grep/alert on.

set -eu

START_SCRIPT="/opt/phoenix/start-phoenix.sh"
HOLIDAYS_FILE="/opt/phoenix/nse-holidays.txt"
BACKEND_CONTAINER="phoenix-oci-backend"
LOG_TAG="verify-morning-start"

log()   { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [$LOG_TAG] $*"; }
fatal() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [$LOG_TAG] FATAL: $*"; }

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

if [ -f "$HOLIDAYS_FILE" ]; then
    while IFS= read -r line; do
        clean=$(echo "$line" | sed 's/#.*//' | tr -d ' \t\r')
        [ -z "$clean" ] && continue
        if [ "$clean" = "$TODAY_IST" ]; then
            log "Today ($TODAY_IST) is an NSE holiday — verify skipped."
            exit 0
        fi
    done < "$HOLIDAYS_FILE"
fi

is_healthy() {
    status=$(docker ps --filter "name=${BACKEND_CONTAINER}" --format "{{.Status}}" 2>/dev/null || echo "")
    case "$status" in
        *healthy*) return 0 ;;
        *)         return 1 ;;
    esac
}

if is_healthy; then
    log "Backend healthy at verify time — start succeeded as scheduled."
    exit 0
fi

log "Backend not healthy at 09:10 IST — invoking $START_SCRIPT for one self-heal attempt."
if [ -x "$START_SCRIPT" ]; then
    "$START_SCRIPT" || log "Self-heal start exited non-zero — see preceding log."
else
    fatal "Self-heal aborted: $START_SCRIPT missing or not executable."
    exit 1
fi

# start-phoenix.sh already sleeps 90s waiting for healthcheck; re-check now.
if is_healthy; then
    log "Backend healthy after self-heal — recovered."
    exit 0
fi

fatal "Backend STILL not healthy after self-heal on trading day $TODAY_IST. Operator action required: docker logs ${BACKEND_CONTAINER} --tail 100"
exit 1
