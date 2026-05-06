#!/bin/sh
# Manual deploy: pull latest code and extend bind mounts for EMA20 profit-booking files.
# Run as opc on the OCI VM via the bastion.
set -eu

echo "=== Pull latest code ==="
sudo git config --global --add safe.directory /opt/phoenix/app 2>/dev/null || true
sudo git -C /opt/phoenix/app pull origin main 2>&1 | tail -5

echo
echo "=== Current git head ==="
git -C /opt/phoenix/app log --oneline -3

echo
echo "=== Update phoenix-override.yml with EMA20 bind mounts ==="
OVERRIDE=/opt/phoenix/phoenix-override.yml
# Add bind mounts for the new EMA20 strategy files (PHX#182/#183/#184/#186)
# Only add them if not already present.
for f in \
    "app/strategies/ema20_strategy.py" \
    "app/strategies/ema20_config.py" \
    "app/runners/multi_instrument_stream.py"; do
    if ! sudo grep -q "$f" "$OVERRIDE"; then
        echo "  Adding bind mount for $f"
        sudo sed -i \
            "s|      - \"/opt/phoenix/app/app/config/settings.py:/app/app/config/settings.py:ro\"|      - \"/opt/phoenix/app/app/config/settings.py:/app/app/config/settings.py:ro\"\n      - \"/opt/phoenix/app/$f:/app/$f:ro\"|" \
            "$OVERRIDE"
    else
        echo "  $f already mounted"
    fi
done

echo
echo "=== Current backend bind mounts ==="
sudo grep -A 30 "volumes:" "$OVERRIDE" | grep "/app/" | head -20

echo
echo "=== Restart backend with new bind mounts ==="
CONTROL_PLANE_PG_PASSWORD_HOST=dummy \
  docker compose \
    -f /opt/phoenix/app/docker-compose.oci-live.yml \
    -f /opt/phoenix/phoenix-override.yml \
    --env-file /opt/phoenix/phoenix-deploy.env \
    up -d --no-deps --force-recreate backend

echo
echo "=== Wait 30s for health check ==="
sleep 30
docker ps --filter name=phoenix-oci-backend --format "table {{.Names}}\t{{.Status}}"

echo
echo "=== Verify new code is in container ==="
docker exec phoenix-oci-backend grep -c "_emit_exit_attribution\|tp1_filled\|giveback_armed" /app/app/strategies/ema20_strategy.py 2>&1 | head -3 || echo "  grep failed — checking file:"
docker exec phoenix-oci-backend head -5 /app/app/strategies/ema20_strategy.py 2>&1 | head -5
