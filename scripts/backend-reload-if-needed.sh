#!/bin/sh
# Restart the backend after optimizer promotion only when broker state is flat.

set -eu

APP_DIR="${PHOENIX_APP_DIR:-/opt/phoenix/app}"
COMPOSE_FILE="${PHOENIX_COMPOSE_FILE:-$APP_DIR/docker-compose.oci-live.yml}"
OVERRIDE_FILE="${PHOENIX_OVERRIDE_FILE:-/opt/phoenix/phoenix-override.yml}"
ENV_FILE="${PHOENIX_ENV_FILE:-/opt/phoenix/phoenix-deploy.env}"
STATE_DIR="${PHOENIX_STATE_HOST_PATH:-/opt/phoenix/state}"
LOCK_FILE="${BACKEND_RELOAD_LOCK_FILE:-$STATE_DIR/backend-reload.lock}"
LOG_TAG="backend-reload"
BACKEND_RUNTIME_UID="${PHOENIX_BACKEND_RUNTIME_UID:-100}"
BACKEND_RUNTIME_GID="${PHOENIX_BACKEND_RUNTIME_GID:-101}"
LOG_DIR="${PHOENIX_LOG_HOST_PATH:-/opt/phoenix/logs}"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [$LOG_TAG] $*"; }

fatal() {
    log "FATAL: $*"
    exit 1
}

if [ "${OPTIMIZER_BACKEND_RELOAD_DISABLED:-false}" = "true" ]; then
    log "backend reload disabled by OPTIMIZER_BACKEND_RELOAD_DISABLED=true"
    exit 0
fi

command -v docker >/dev/null 2>&1 || fatal "docker CLI is not available."
[ -f "$COMPOSE_FILE" ] || fatal "compose file missing: $COMPOSE_FILE"
[ -f "$OVERRIDE_FILE" ] || fatal "compose override missing: $OVERRIDE_FILE"
[ -f "$ENV_FILE" ] || fatal "deploy env file missing: $ENV_FILE"
mkdir -p "$STATE_DIR" "$LOG_DIR"
chown -R "$BACKEND_RUNTIME_UID:$BACKEND_RUNTIME_GID" "$STATE_DIR" "$LOG_DIR"
chmod 700 "$STATE_DIR"
chmod 755 "$LOG_DIR"

if [ "${BACKEND_RELOAD_LOCK_HELD:-false}" != "true" ]; then
    if command -v flock >/dev/null 2>&1; then
        env BACKEND_RELOAD_LOCK_HELD=true flock -n "$LOCK_FILE" "$0" "$@"
        exit $?
    fi

    LOCK_DIR="${LOCK_FILE}.d"
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        fatal "backend reload fallback lock is already held: $LOCK_DIR"
    fi
    trap 'rmdir "$LOCK_DIR"' EXIT INT TERM
fi

docker ps --filter name=phoenix-oci-backend --filter status=running -q \
    | grep -q . || fatal "phoenix-oci-backend is not running."

PROMOTED_IDS=$(
    docker exec phoenix-oci-backend python - <<'PY'
from __future__ import annotations

from app.data.postgres import connect_with_retry, get_control_plane_dsn

SQL = """
SELECT candidate_id
  FROM public.strategy_config_candidates
 WHERE status = 'promoted'
   AND reviewed_at >= (
       (date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') - interval '1 day')
       AT TIME ZONE 'Asia/Kolkata'
   )
 ORDER BY reviewed_at DESC
 LIMIT 50
"""

with connect_with_retry(get_control_plane_dsn(), autocommit=True) as conn:
    with conn.cursor() as cur:
        cur.execute(SQL)
        rows = cur.fetchall() or []

print(",".join(str(row[0]) for row in rows))
PY
) || fatal "failed to query promoted strategy_config_candidates."

PROMOTED_IDS=$(printf '%s' "$PROMOTED_IDS" | tr -d '\r\n')
if [ -z "$PROMOTED_IDS" ]; then
    log "no promoted candidates from the previous IST day; backend reload skipped."
    exit 0
fi

docker exec phoenix-oci-backend python - <<'PY' || fatal "open positions or orders detected; refusing backend reload."
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.getenv("PHOENIX_RELOAD_ADMIN_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
ADMIN_API_KEY = (os.getenv("ADMIN_API_KEY") or os.getenv("PHOENIX_ADMIN_API_KEY") or "").strip()
TERMINAL_ORDER_STATUSES = {
    "complete",
    "completed",
    "cancelled",
    "canceled",
    "rejected",
    "failed",
    "expired",
}

if not ADMIN_API_KEY:
    sys.exit("ADMIN_API_KEY is required for backend reload hub-state checks")


def request_json(path: str, *, tenant_id: str | None = None) -> dict:
    headers = {
        "Accept": "application/json",
        "X-Admin-Key": ADMIN_API_KEY,
    }
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id
    req = urllib.request.Request(f"{BASE_URL}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{path} returned HTTP {exc.code}: {body[:300]}") from exc


def numeric_field(item: dict, *keys: str) -> float:
    for key in keys:
        value = item.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


accounts = request_json("/admin/broker-accounts").get("broker_accounts") or []
exposures: list[dict[str, object]] = []

for account in accounts:
    if not isinstance(account, dict):
        continue
    tenant_id = str(account.get("tenant_id") or "").strip()
    account_id = str(account.get("broker_account_id") or "").strip()
    if not tenant_id or not account_id:
        continue
    account_path = urllib.parse.quote(account_id, safe="")

    positions = request_json(
        f"/tenant/me/accounts/{account_path}/positions",
        tenant_id=tenant_id,
    ).get("positions") or []
    for position in positions:
        if not isinstance(position, dict):
            continue
        qty = numeric_field(position, "quantity", "netqty", "net_quantity", "net_qty", "qty")
        if abs(qty) > 0:
            exposures.append({
                "kind": "position",
                "tenant_id": tenant_id,
                "broker_account_id": account_id,
                "symbol": position.get("symbol"),
                "quantity": qty,
            })

    orders = request_json(
        f"/tenant/me/accounts/{account_path}/orders",
        tenant_id=tenant_id,
    ).get("orders") or []
    for order in orders:
        if not isinstance(order, dict):
            continue
        order_status = str(order.get("status") or "").strip().lower()
        if order_status not in TERMINAL_ORDER_STATUSES:
            exposures.append({
                "kind": "order",
                "tenant_id": tenant_id,
                "broker_account_id": account_id,
                "order_id": order.get("order_id") or order.get("broker_order_id"),
                "status": order_status or "unknown",
            })

if exposures:
    print(json.dumps(exposures[:20], sort_keys=True), file=sys.stderr)
    sys.exit(2)

print("flat")
PY

log "promoted candidates found: $PROMOTED_IDS; restarting backend."

CONTROL_PLANE_PG_PASSWORD_HOST="${CONTROL_PLANE_PG_PASSWORD_HOST:-dummy}" \
docker compose \
    -f "$COMPOSE_FILE" \
    -f "$OVERRIDE_FILE" \
    --env-file "$ENV_FILE" \
    restart backend

LAST_STATUS="unknown"
for _attempt in $(seq 1 60); do
    LAST_STATUS=$(
        docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
            phoenix-oci-backend 2>/dev/null || true
    )
    if [ "$LAST_STATUS" = "healthy" ]; then
        log "backend healthy after reload."
        exit 0
    fi
    sleep 1
done

fatal "backend did not become healthy within 60s after reload; last status=$LAST_STATUS"
