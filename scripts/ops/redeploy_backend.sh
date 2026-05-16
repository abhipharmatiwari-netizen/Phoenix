#!/bin/sh
# Pull the images pinned in phoenix-deploy.env and restart the web stack.
# Run this after build_and_push_image.sh or build_push_ip.sh has pushed a new image.
#
# Usage: sh scripts/ops/redeploy_backend.sh
#
# The IMAGE_TAG in /opt/phoenix/phoenix-deploy.env determines which image is pulled.
# Verify the tag is the intended git SHA before running.
#
# CONTROL_PLANE_PG_PASSWORD_HOST=dummy satisfies compose interpolation for the
# migrator service; the backend reads the real password from /run/secrets/.
set -eu

APP_DIR="/opt/phoenix/app"
COMPOSE_FILE="${APP_DIR}/docker-compose.oci-live.yml"
OVERRIDE_FILE="/opt/phoenix/phoenix-override.yml"
ENV_FILE="/opt/phoenix/phoenix-deploy.env"
HEALTH_TIMEOUT=120
HEALTH_INTERVAL=10

echo "=== Current IMAGE_TAG ==="
grep "IMAGE_TAG" "$ENV_FILE"

echo
printf "Deploy this image? [y/N] "
read -r CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
  echo "Aborted."
  exit 0
fi

echo
echo "=== Pull new image pair ==="
CONTROL_PLANE_PG_PASSWORD_HOST=dummy \
  docker compose \
    -f "$COMPOSE_FILE" \
    -f "$OVERRIDE_FILE" \
    --env-file "$ENV_FILE" \
    pull backend nginx

echo
echo "=== Restart backend ==="
CONTROL_PLANE_PG_PASSWORD_HOST=dummy \
  docker compose \
    -f "$COMPOSE_FILE" \
    -f "$OVERRIDE_FILE" \
    --env-file "$ENV_FILE" \
    up -d --no-deps --no-build --force-recreate backend

echo
echo "=== Wait for /readyz (up to ${HEALTH_TIMEOUT}s) ==="
elapsed=0
while [ "$elapsed" -lt "$HEALTH_TIMEOUT" ]; do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/readyz 2>/dev/null || echo "000")
  if [ "$STATUS" = "200" ]; then
    echo "  /readyz OK after ${elapsed}s"
    break
  fi
  printf "  %ds — /readyz returned %s, waiting...\n" "$elapsed" "$STATUS"
  sleep "$HEALTH_INTERVAL"
  elapsed=$((elapsed + HEALTH_INTERVAL))
done

if [ "$STATUS" != "200" ]; then
  echo "ERROR: /readyz did not return 200 within ${HEALTH_TIMEOUT}s (last: $STATUS)."
  echo "Check logs: docker compose -f $COMPOSE_FILE logs --tail 100 backend"
  exit 1
fi

echo
echo "=== Recreate nginx on the same IMAGE_TAG ==="
CONTROL_PLANE_PG_PASSWORD_HOST=dummy \
  docker compose \
    -f "$COMPOSE_FILE" \
    -f "$OVERRIDE_FILE" \
    --env-file "$ENV_FILE" \
    up -d --no-deps --no-build --force-recreate nginx

echo
echo "=== Running container image ==="
docker inspect phoenix-oci-backend --format "{{.Config.Image}}"
docker inspect phoenix-oci-web --format "{{.Config.Image}}"
docker ps --filter name=phoenix-oci --format "table {{.Names}}\t{{.Status}}"

echo
echo "Redeploy complete."
