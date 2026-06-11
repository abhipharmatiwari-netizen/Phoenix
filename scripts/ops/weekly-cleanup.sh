#!/bin/bash
# /opt/phoenix/scripts/weekly-cleanup.sh
# Weekly OCI VM cleanup. Does not remove volumes, backups, or secrets.

set -uo pipefail

PHOENIX_ROOT="${PHOENIX_ROOT:-/opt/phoenix}"
LOG="${PHOENIX_CLEANUP_LOG:-$PHOENIX_ROOT/logs/cron-cleanup.log}"
KEEP_LIVE_TAGS="${PHOENIX_CLEANUP_KEEP_LIVE_TAGS:-3}"
LOG_RETENTION_DAYS="${PHOENIX_LOG_RETENTION_DAYS:-7}"
SCRIP_MASTER_KEEP="${PHOENIX_SCRIP_MASTER_KEEP:-1}"
DRY_RUN="${PHOENIX_CLEANUP_DRY_RUN:-false}"

TS() { date "+%Y-%m-%d %H:%M:%S IST"; }
log() { echo "[$(TS)] $*" | tee -a "$LOG"; }
logq() { echo "[$(TS)] $*" >> "$LOG"; }

run_cmd() {
  if [ "$DRY_RUN" = "true" ]; then
    log "dry-run: $*"
    return 0
  fi
  "$@" >> "$LOG" 2>&1
}

active_images_file="$(mktemp)"
trap 'rm -f "$active_images_file"' EXIT

docker ps --format "{{.Image}}" | sort -u > "$active_images_file"

is_active_image() {
  grep -Fxq "$1" "$active_images_file"
}

prune_image_if_safe() {
  image="$1"
  if is_active_image "$image"; then
    log "  preserving active image $image"
    return 0
  fi
  log "  removing stale image $image"
  run_cmd docker rmi "$image" || true
}

log "=== weekly-cleanup started ==="
log "configuration dry_run=$DRY_RUN keep_live_tags=$KEEP_LIVE_TAGS log_retention_days=$LOG_RETENTION_DAYS"

log "pre-cleanup docker system df:"
docker system df 2>&1 | while IFS= read -r line; do logq "  $line"; done
log "active images:"
while IFS= read -r image; do
  [ -n "$image" ] || continue
  logq "  $image"
done < "$active_images_file"

log "stopped containers..."
run_cmd docker container prune -f

log "dangling images..."
run_cmd docker image prune -f

log "old local Phoenix rollback images..."
for repo in phoenix-local-backend phoenix-local-nginx phoenix-oi-ml-shadow aurelium; do
  docker images --format "{{.Repository}}:{{.Tag}}" "$repo" 2>/dev/null \
    | grep -E ':(local-|live-|oi-ml-shadow-)?[0-9a-f]{7,40}$|:live-' \
    | sort -r \
    | awk -v keep="$KEEP_LIVE_TAGS" 'NR > keep {print}' \
    | while IFS= read -r image; do
        [ -n "$image" ] || continue
        prune_image_if_safe "$image"
      done
done

log "build cache..."
run_cmd docker buildx prune -f
run_cmd docker builder prune -f

log "old log directories (> ${LOG_RETENTION_DAYS} days)..."
CUTOFF="$(date -d "${LOG_RETENTION_DAYS} days ago" +%Y-%m-%d)"
find "$PHOENIX_ROOT/logs" -maxdepth 1 -type d -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' \
  | while IFS= read -r dir; do
      dirdate="$(basename "$dir")"
      if [[ "$dirdate" < "$CUTOFF" ]]; then
        log "  removing old log dir $dir"
        run_cmd rm -rf "$dir"
      fi
    done

log "old scrip_master files..."
find "$PHOENIX_ROOT/logs" -maxdepth 1 -type f -name 'scrip_master_*.json' \
  | sort -r \
  | awk -v keep="$SCRIP_MASTER_KEEP" 'NR > keep {print}' \
  | while IFS= read -r file; do
      [ -n "$file" ] || continue
      log "  removing old scrip master $file"
      run_cmd rm -f "$file"
    done

log "post-cleanup docker system df:"
docker system df 2>&1 | while IFS= read -r line; do logq "  $line"; done
AVAIL="$(df -h / | awk 'NR==2 {print $4}')"
USED="$(df -h / | awk 'NR==2 {print $3}')"
log "  disk used=${USED} avail=${AVAIL}"
log "=== weekly-cleanup done ==="
