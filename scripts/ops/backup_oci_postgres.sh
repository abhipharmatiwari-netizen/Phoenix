#!/bin/bash
# VM-local Phoenix Postgres backup.
#
# Intended cron schedule on the OCI VM:
#   0 18 * * 1-5 root /opt/phoenix/scripts/backup-postgres.sh
# This is 23:30 IST Monday-Friday. The script writes only to the local VM backup
# directory; off-host replication must be handled by a separate approved job.

set -uo pipefail

PHOENIX_ROOT="${PHOENIX_ROOT:-/opt/phoenix}"
BACKUP_DIR="${PHOENIX_PG_BACKUP_DIR:-$PHOENIX_ROOT/backups/postgres}"
LOG="${PHOENIX_PG_BACKUP_LOG:-$PHOENIX_ROOT/logs/phoenix-postgres-backup.log}"
LOCK_FILE="${PHOENIX_PG_BACKUP_LOCK:-$PHOENIX_ROOT/state/phoenix-postgres-backup.lock}"
CONTAINER="${PHOENIX_PG_CONTAINER:-phoenix-oci-postgres}"
DB_NAME="${PHOENIX_PG_DATABASE:-phoenix}"
DB_USER="${PHOENIX_PG_USER:-phoenix_app}"
KEEP_DAYS="${PHOENIX_PG_BACKUP_KEEP_DAYS:-14}"
MIN_FREE_GB="${PHOENIX_PG_BACKUP_MIN_FREE_GB:-10}"
DRY_RUN="${PHOENIX_PG_BACKUP_DRY_RUN:-false}"

install -d -m 700 "$BACKUP_DIR" "$PHOENIX_ROOT/state"
install -d -m 755 "$(dirname "$LOG")"
exec >> "$LOG" 2>&1

ts() { date -u "+%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*"; }

as_int_at_least() {
  value="$1"
  default="$2"
  minimum="$3"
  case "$value" in
    ''|*[!0-9]*) echo "$default" ;;
    *)
      if [ "$value" -lt "$minimum" ]; then
        echo "$default"
      else
        echo "$value"
      fi
      ;;
  esac
}

KEEP_DAYS="$(as_int_at_least "$KEEP_DAYS" 14 1)"
MIN_FREE_GB="$(as_int_at_least "$MIN_FREE_GB" 10 1)"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "backup already running; exiting"
  exit 0
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  log "ERROR: Postgres container not found: $CONTAINER"
  exit 1
fi

if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
  log "ERROR: Postgres container is not running: $CONTAINER"
  exit 1
fi

free_gb="$(df -Pk "$BACKUP_DIR" | awk 'NR == 2 {printf "%d", $4 / 1048576}')"
if [ "${free_gb:-0}" -lt "$MIN_FREE_GB" ]; then
  log "ERROR: insufficient free space for backup path=$BACKUP_DIR free_gb=${free_gb:-0} min_free_gb=$MIN_FREE_GB"
  exit 1
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_file="$BACKUP_DIR/phoenix_${stamp}.dump"
tmp_file="$backup_file.tmp"
evidence_file="$BACKUP_DIR/latest.json"

cleanup_tmp() {
  rm -f "$tmp_file" "$evidence_file.tmp"
}
trap cleanup_tmp EXIT

log "backup started container=$CONTAINER database=$DB_NAME path=$backup_file keep_days=$KEEP_DAYS free_gb=$free_gb dry_run=$DRY_RUN"

if [ "$DRY_RUN" = "true" ]; then
  if ! docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc --schema-only > "$tmp_file"; then
    log "ERROR: dry-run schema-only pg_dump failed"
    exit 1
  fi
  chmod 600 "$tmp_file"

  if ! docker exec -i "$CONTAINER" pg_restore -l < "$tmp_file" >/dev/null; then
    log "ERROR: dry-run pg_restore list verification failed"
    exit 1
  fi
  log "dry-run: schema-only pg_dump and pg_restore verification passed; no full backup created"
else
  if ! docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$tmp_file"; then
    log "ERROR: pg_dump failed"
    exit 1
  fi
  chmod 600 "$tmp_file"

  if ! docker exec -i "$CONTAINER" pg_restore -l < "$tmp_file" >/dev/null; then
    log "ERROR: pg_restore list verification failed"
    exit 1
  fi

  mv "$tmp_file" "$backup_file"
  size_bytes="$(stat -c '%s' "$backup_file" 2>/dev/null || echo 0)"
  size_human="$(du -h "$backup_file" | awk '{print $1}')"

  cat > "$evidence_file.tmp" <<EOF
{
  "backup_schedule": {
    "enabled": true,
    "cron": "0 18 * * 1-5",
    "timezone": "UTC",
    "local_time": "23:30 IST Monday-Friday",
    "container": "$CONTAINER",
    "database": "$DB_NAME",
    "last_backup_file": "$backup_file",
    "last_backup_size": "$size_human",
    "last_backup_size_bytes": $size_bytes,
    "retention": {
      "local_keep_days": $KEEP_DAYS,
      "dry_run": false
    },
    "verified_at": "$(ts)"
  }
}
EOF
  chmod 600 "$evidence_file.tmp"
  mv "$evidence_file.tmp" "$evidence_file"
  log "backup complete file=$backup_file size=$size_human verified=true"
fi

log "retention started keep_days=$KEEP_DAYS"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'phoenix_*.dump' -mtime +"$KEEP_DAYS" -print \
  | while IFS= read -r old_file; do
      [ -n "$old_file" ] || continue
      if [ "$DRY_RUN" = "true" ]; then
        log "dry-run: would remove old backup $old_file"
      else
        rm -f "$old_file"
        log "removed old backup $old_file"
      fi
    done
log "retention complete"
