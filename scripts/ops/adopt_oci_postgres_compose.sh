#!/bin/sh
# Adopt the VM-local Postgres container into the phoenix-oci-live compose profile.
# Requires a maintenance window and an external backup/rollback plan.

set -eu

if [ "${CONFIRM_POSTGRES_RECREATE:-}" != "YES" ]; then
  echo "ERROR: set CONFIRM_POSTGRES_RECREATE=YES during an approved maintenance window" >&2
  exit 1
fi

APP_DIR="${APP_DIR:-/opt/phoenix/app}"
ENV_FILE="${ENV_FILE:-/opt/phoenix/phoenix-deploy.env}"
OVERRIDE_FILE="${OVERRIDE_FILE:-/opt/phoenix/phoenix-override.yml}"
COMPOSE_FILE="${COMPOSE_FILE:-$APP_DIR/docker-compose.oci-live.yml}"
PGDATA_HOST_PATH="${PHOENIX_PGDATA_HOST_PATH:-/opt/phoenix/pgdata}"

if [ ! -f "$PGDATA_HOST_PATH/PG_VERSION" ]; then
  echo "ERROR: PGDATA path does not look initialized: $PGDATA_HOST_PATH" >&2
  exit 1
fi

cd "$APP_DIR"

echo "Stopping Phoenix backend/watchdog before Postgres container adoption"
CONTROL_PLANE_PG_PASSWORD_HOST="${CONTROL_PLANE_PG_PASSWORD_HOST:-dummy}" \
  docker compose \
    -f "$COMPOSE_FILE" \
    -f "$OVERRIDE_FILE" \
    --env-file "$ENV_FILE" \
    stop backend-watchdog backend || true

echo "Recreating phoenix-oci-postgres under compose profile vm-local-postgres"
docker stop phoenix-oci-postgres >/dev/null 2>&1 || true
docker rm phoenix-oci-postgres >/dev/null 2>&1 || true

CONTROL_PLANE_PG_PASSWORD_HOST="${CONTROL_PLANE_PG_PASSWORD_HOST:-dummy}" \
  PHOENIX_PGDATA_HOST_PATH="$PGDATA_HOST_PATH" \
  docker compose \
    --profile vm-local-postgres \
    -f "$COMPOSE_FILE" \
    -f "$OVERRIDE_FILE" \
    --env-file "$ENV_FILE" \
    up -d --no-deps postgres

for _ in 1 2 3 4 5 6 7 8 9 10; do
  status=$(docker inspect phoenix-oci-postgres --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}')
  echo "Postgres health: $status"
  [ "$status" = "healthy" ] && exit 0
  sleep 3
done

echo "ERROR: phoenix-oci-postgres did not become healthy" >&2
exit 1
