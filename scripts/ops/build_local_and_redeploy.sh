#!/bin/sh
# Build a local Docker image on the VM and switch to it — no OCIR push needed.
# This permanently removes the bind-mount overrides from phoenix-override.yml.
set -eu

APP_DIR="/opt/phoenix/app"
GIT_SHA=$(git -C "$APP_DIR" rev-parse --short HEAD)
LOCAL_TAG="phoenix-local-backend:${GIT_SHA}"

echo "=== Build config ==="
echo "SHA:    $GIT_SHA"
echo "Image:  $LOCAL_TAG"
echo "App:    $APP_DIR"

echo
echo "=== Building image (this takes 5-10 min on first build) ==="
sudo docker build \
    -t "$LOCAL_TAG" \
    -f "${APP_DIR}/Dockerfile" \
    "${APP_DIR}" 2>&1

echo
echo "=== Remove EMA20 bind mounts from phoenix-override.yml ==="
OVERRIDE=/opt/phoenix/phoenix-override.yml
sudo cp "$OVERRIDE" "${OVERRIDE}.bak.$(date +%Y%m%d_%H%M%S)"

# Remove the three EMA20 bind mount lines we added
sudo sed -i \
    '/app\/runners\/multi_instrument_stream.py/d' \
    "$OVERRIDE"
sudo sed -i \
    '/app\/strategies\/ema20_config.py/d' \
    "$OVERRIDE"
sudo sed -i \
    '/app\/strategies\/ema20_strategy.py/d' \
    "$OVERRIDE"

echo "=== Updated bind mounts in override ==="
sudo grep "volumes:" -A 20 "$OVERRIDE" | grep "/app/" | head -15

echo
echo "=== Switch backend image to local build in deploy.env ==="
sudo sed -i "s|^IMAGE_TAG=.*|IMAGE_TAG=local-${GIT_SHA}|" /opt/phoenix/phoenix-deploy.env

# Also update the compose override to use the local image name
# Add image override to phoenix-override.yml backend service
if ! sudo grep -q "image:.*phoenix-local-backend" "$OVERRIDE"; then
    sudo sed -i "s|  backend:|  backend:\n    image: ${LOCAL_TAG}|" "$OVERRIDE"
fi

echo "=== IMAGE_TAG in phoenix-deploy.env ==="
grep "IMAGE_TAG" /opt/phoenix/phoenix-deploy.env

echo
echo "=== Redeploy backend with local image ==="
CONTROL_PLANE_PG_PASSWORD_HOST=dummy \
  sudo docker compose \
    -f "${APP_DIR}/docker-compose.oci-live.yml" \
    -f /opt/phoenix/phoenix-override.yml \
    --env-file /opt/phoenix/phoenix-deploy.env \
    up -d --no-deps --force-recreate backend

echo
echo "=== Wait 90s for health check ==="
sleep 90

echo
echo "=== Final status ==="
docker ps --filter name=phoenix-oci-backend --format "table {{.Names}}\t{{.Status}}"
echo
echo "=== Verify code in new container ==="
docker exec phoenix-oci-backend grep -c "tp1_filled\|giveback_armed\|_emit_exit_attribution" /app/app/strategies/ema20_strategy.py && echo "New EMA20 code confirmed"
