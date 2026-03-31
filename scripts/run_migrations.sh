#!/bin/sh
set -eu

MODE="${1:-}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-/migrations}"
APP_NAME="${PHX_MIGRATION_APP_NAME:-phoenix-live-compose}"

require_env() {
  var_name="$1"
  eval "var_value=\${$var_name:-}"
  if [ -z "$var_value" ]; then
    echo "Missing required environment variable: $var_name" >&2
    exit 1
  fi
}

sql_quote() {
  printf "%s" "$1" | sed "s/'/''/g"
}

wait_for_postgres() {
  until pg_isready -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" >/dev/null 2>&1; do
    echo "Waiting for Postgres $PGHOST:$PGPORT/$PGDATABASE ..."
    sleep 2
  done
}

list_migration_files() {
  find "$MIGRATIONS_DIR" -maxdepth 1 -type f -name '*.sql' -print | LC_ALL=C sort
}

ensure_migrations_dir() {
  if [ ! -d "$MIGRATIONS_DIR" ]; then
    echo "Migrations directory not found: $MIGRATIONS_DIR" >&2
    exit 1
  fi
}

ensure_tracking_table() {
  psql -v ON_ERROR_STOP=1 <<SQL
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version TEXT PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    checksum_sha256 TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by TEXT NOT NULL DEFAULT CURRENT_USER,
    app_name TEXT NOT NULL
);
SQL
}

check_tracking_table_exists() {
  exists="$(psql -v ON_ERROR_STOP=1 -tA -c "SELECT to_regclass('public.schema_migrations') IS NOT NULL")"
  if [ "$exists" != "t" ]; then
    echo "Missing public.schema_migrations; migration apply step was not run against the target database." >&2
    exit 1
  fi
}

apply_migrations() {
  ensure_tracking_table

  file_list="$(mktemp)"
  list_migration_files >"$file_list"
  if [ ! -s "$file_list" ]; then
    echo "No migration files found in $MIGRATIONS_DIR" >&2
    rm -f "$file_list"
    exit 1
  fi

  while IFS= read -r migration_file; do
    filename="$(basename "$migration_file")"
    version="${filename%.sql}"
    checksum="$(sha256sum "$migration_file" | awk '{print $1}')"
    quoted_version="$(sql_quote "$version")"
    row="$(psql -v ON_ERROR_STOP=1 -tA -F '|' -c "SELECT filename, checksum_sha256 FROM public.schema_migrations WHERE version = '$quoted_version'")"
    if [ -n "$row" ]; then
      applied_filename="$(printf "%s" "$row" | cut -d'|' -f1)"
      applied_checksum="$(printf "%s" "$row" | cut -d'|' -f2)"
      if [ "$applied_filename" != "$filename" ] || [ "$applied_checksum" != "$checksum" ]; then
        echo "Migration tracking mismatch for $filename; expected checksum $checksum but found $applied_filename / $applied_checksum" >&2
        rm -f "$file_list"
        exit 1
      fi
      echo "Migration already applied: $filename"
      continue
    fi

    echo "Applying migration: $filename"
    psql -v ON_ERROR_STOP=1 -1 -f "$migration_file"
    psql -v ON_ERROR_STOP=1 <<SQL
INSERT INTO public.schema_migrations (
    version,
    filename,
    checksum_sha256,
    app_name
) VALUES (
    '$(sql_quote "$version")',
    '$(sql_quote "$filename")',
    '$(sql_quote "$checksum")',
    '$(sql_quote "$APP_NAME")'
);
SQL
  done <"$file_list"

  rm -f "$file_list"
  echo "Migration apply complete."
}

verify_migrations() {
  check_tracking_table_exists

  file_list="$(mktemp)"
  list_migration_files >"$file_list"
  if [ ! -s "$file_list" ]; then
    echo "No migration files found in $MIGRATIONS_DIR" >&2
    rm -f "$file_list"
    exit 1
  fi

  while IFS= read -r migration_file; do
    filename="$(basename "$migration_file")"
    version="${filename%.sql}"
    checksum="$(sha256sum "$migration_file" | awk '{print $1}')"
    quoted_version="$(sql_quote "$version")"
    row="$(psql -v ON_ERROR_STOP=1 -tA -F '|' -c "SELECT filename, checksum_sha256 FROM public.schema_migrations WHERE version = '$quoted_version'")"
    if [ -z "$row" ]; then
      echo "Missing applied migration record for $filename; cutover is blocked until migrations are run." >&2
      rm -f "$file_list"
      exit 1
    fi
    applied_filename="$(printf "%s" "$row" | cut -d'|' -f1)"
    applied_checksum="$(printf "%s" "$row" | cut -d'|' -f2)"
    if [ "$applied_filename" != "$filename" ] || [ "$applied_checksum" != "$checksum" ]; then
      echo "Applied migration record mismatch for $filename; expected checksum $checksum but found $applied_filename / $applied_checksum" >&2
      rm -f "$file_list"
      exit 1
    fi
  done <"$file_list"

  rm -f "$file_list"
  echo "Migration verification passed for $(list_migration_files | wc -l | awk '{print $1}') file(s)."
}

require_env PGHOST
require_env PGPORT
require_env PGDATABASE
require_env PGUSER
require_env PGPASSWORD
ensure_migrations_dir
wait_for_postgres

case "$MODE" in
  apply)
    apply_migrations
    ;;
  verify)
    verify_migrations
    ;;
  *)
    echo "Usage: sh run_migrations.sh [apply|verify]" >&2
    exit 1
    ;;
esac
