#!/bin/bash
# Apply runtime CPU, memory, and PID caps to known co-tenant containers.
# This script is intentionally idempotent and does not stop or recreate containers.

set -uo pipefail

LOG="${COTENANT_RESOURCE_CAP_LOG:-/opt/phoenix/logs/cotenant-resource-caps.log}"
PREFIX="${COTENANT_CONTAINER_PREFIX:-aurelium-}"
FAILURES=0

TS() { date "+%Y-%m-%d %H:%M:%S %Z"; }
log() { echo "[$(TS)] $*" | tee -a "$LOG"; }

container_exists() {
  docker inspect "$1" >/dev/null 2>&1
}

container_running() {
  [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]
}

apply_cap() {
  name="$1"
  cpus="$2"
  memory="$3"
  pids="$4"

  if ! container_exists "$name"; then
    log "skip missing container name=$name"
    return 0
  fi
  if ! container_running "$name"; then
    log "skip stopped container name=$name"
    return 0
  fi

  log "apply name=$name cpus=$cpus memory=$memory pids=$pids"
  if docker update \
    --cpus "$cpus" \
    --memory "$memory" \
    --memory-swap "$memory" \
    --pids-limit "$pids" \
    "$name" >> "$LOG" 2>&1; then
    return 0
  fi

  log "error applying cap name=$name"
  FAILURES=$((FAILURES + 1))
}

mkdir -p "$(dirname "$LOG")"
log "=== co-tenant resource cap enforcement started ==="

apply_cap "${PREFIX}api-1" "2.0" "6g" "1024"
apply_cap "${PREFIX}clickhouse-1" "2.0" "6g" "2048"
apply_cap "${PREFIX}postgres-1" "1.5" "4g" "1024"
apply_cap "${PREFIX}minio-1" "1.0" "2g" "512"
apply_cap "${PREFIX}prometheus-1" "0.75" "1g" "512"
apply_cap "${PREFIX}grafana-1" "0.5" "1g" "512"
apply_cap "${PREFIX}redis-1" "0.5" "512m" "256"
apply_cap "${PREFIX}nginx-1" "0.5" "256m" "256"
apply_cap "${PREFIX}otel-collector-1" "0.75" "512m" "512"
apply_cap "${PREFIX}alertmanager-1" "0.25" "256m" "256"
apply_cap "${PREFIX}minio-init-1" "0.25" "256m" "128"

if [ "$FAILURES" -gt 0 ]; then
  log "=== co-tenant resource cap enforcement failed failures=$FAILURES ==="
  exit 1
fi

log "=== co-tenant resource cap enforcement done ==="
