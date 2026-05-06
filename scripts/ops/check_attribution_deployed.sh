#!/bin/sh
# Verify PHX#182/183/184/186 code is actually running in the live phoenix-oci-backend container.
# Read-only — no state changes.
set -eu

echo "=== Container image digest ==="
docker inspect phoenix-oci-backend --format '{{.Image}}' 2>&1
docker inspect phoenix-oci-backend --format '{{.Config.Image}}' 2>&1

echo
echo "=== Look for PHX#186 attribution code in the running container ==="
# The marker is the function _emit_exit_attribution which only exists in the new code.
docker exec phoenix-oci-backend grep -c "_emit_exit_attribution" /app/app/strategies/ema20_strategy.py 2>&1 || echo "  marker NOT found — old image"

echo
echo "=== Look for PHX#182 TP1 code marker ==="
docker exec phoenix-oci-backend grep -c "tp1_filled" /app/app/strategies/ema20_strategy.py 2>&1 || echo "  marker NOT found — old image"

echo
echo "=== Check if exit_attribution.jsonl exists and its size ==="
docker exec phoenix-oci-backend sh -c 'ls -la /app/logs/exit_attribution.jsonl 2>&1 || echo "(file does not exist)"'
docker exec phoenix-oci-backend sh -c 'wc -l /app/logs/exit_attribution.jsonl 2>&1 || true'

echo
echo "=== Mount points / persistence ==="
docker inspect phoenix-oci-backend --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} ({{.Type}}){{println}}{{end}}'
