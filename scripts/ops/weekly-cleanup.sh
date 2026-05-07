#!/bin/bash
# /opt/phoenix/scripts/weekly-cleanup.sh
# Weekly OCI VM cleanup — every Sunday 2:00 AM IST (Saturday 20:30 UTC)
# Managed by: /etc/cron.d/phoenix-cleanup

set -uo pipefail

LOG=/opt/phoenix/logs/cron-cleanup.log
TS()  { date "+%Y-%m-%d %H:%M:%S IST"; }
log() { echo "[$(TS)] $*" | tee -a "$LOG"; }
logq(){ echo "[$(TS)] $*" >> "$LOG"; }

log "=== weekly-cleanup started ==="

# ── 1. Stopped containers ──────────────────────────────────────────────────
log "stopped containers..."
docker container prune -f >> "$LOG" 2>&1

# ── 2. Dangling images ─────────────────────────────────────────────────────
log "dangling images..."
docker image prune -f >> "$LOG" 2>&1

# ── 3. Old aurelium SHA-tagged builds (keep latest + most recent live-*) ──
log "old aurelium SHA builds..."
docker images --format "{{.Repository}}:{{.Tag}}" aurelium 2>/dev/null \
    | grep -E ':[0-9a-f]{40}$' \
    | xargs -r docker rmi >> "$LOG" 2>&1 || true

# Keep only the single most-recent live-* tag; remove the rest
LIVE_TAGS=$(docker images --format "{{.Tag}}" aurelium 2>/dev/null \
    | grep "^live-" | sort -r)
FIRST=1
while IFS= read -r tag; do
    if [ "$FIRST" = "1" ]; then
        FIRST=0
        continue
    fi
    log "  removing aurelium:${tag}"
    docker rmi "aurelium:${tag}" >> "$LOG" 2>&1 || true
done <<< "$LIVE_TAGS"

# ── 4. Build cache ─────────────────────────────────────────────────────────
log "build cache..."
docker buildx prune -f >> "$LOG" 2>&1
docker builder prune -f >> "$LOG" 2>&1

# ── 5. Old phoenix log directories (> 7 days old by directory name) ────────
log "old log directories (> 7 days)..."
CUTOFF=$(date -d "7 days ago" +%Y-%m-%d)
find /opt/phoenix/logs -maxdepth 1 -type d -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' \
    | while read -r dir; do
        dirdate=$(basename "$dir")
        if [[ "$dirdate" < "$CUTOFF" ]]; then
            log "  removing $dir"
            rm -rf "$dir" >> "$LOG" 2>&1 || true
        fi
      done

# ── 6. Old scrip_master files (keep only the most recent) ─────────────────
log "old scrip_master files..."
OLDEST=$(ls -t /opt/phoenix/logs/scrip_master_*.json 2>/dev/null | tail -n +2)
if [ -n "$OLDEST" ]; then
    echo "$OLDEST" | while read -r f; do
        log "  removing $f"
        rm -f "$f" >> "$LOG" 2>&1 || true
    done
fi

# ── 7. Summary ─────────────────────────────────────────────────────────────
log "summary:"
docker system df 2>&1 | while IFS= read -r line; do logq "  $line"; done
AVAIL=$(df -h / | awk 'NR==2 {print $4}')
USED=$(df -h /  | awk 'NR==2 {print $3}')
log "  disk used=${USED} avail=${AVAIL}"
log "=== weekly-cleanup done ==="
