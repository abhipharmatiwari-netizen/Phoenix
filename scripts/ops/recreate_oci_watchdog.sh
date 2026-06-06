#!/bin/sh
# Recreate phoenix-oci-watchdog from the base compose contract and verify no Docker socket.

set -eu

APP_DIR="${APP_DIR:-/opt/phoenix/app}"
ENV_FILE="${ENV_FILE:-/opt/phoenix/phoenix-deploy.env}"
OVERRIDE_FILE="${OVERRIDE_FILE:-/opt/phoenix/phoenix-override.yml}"
COMPOSE_FILE="${COMPOSE_FILE:-$APP_DIR/docker-compose.oci-live.yml}"

cd "$APP_DIR"

docker rm -f phoenix-oci-watchdog >/dev/null 2>&1 || true

CONTROL_PLANE_PG_PASSWORD_HOST="${CONTROL_PLANE_PG_PASSWORD_HOST:-dummy}" \
  docker compose \
    -f "$COMPOSE_FILE" \
    -f "$OVERRIDE_FILE" \
    --env-file "$ENV_FILE" \
    up -d --no-deps backend-watchdog

if docker inspect phoenix-oci-watchdog --format '{{json .Mounts}}' | grep -q '/var/run/docker.sock'; then
  echo "ERROR: phoenix-oci-watchdog still has Docker socket mounted" >&2
  exit 1
fi

echo "phoenix-oci-watchdog recreated without Docker socket"
