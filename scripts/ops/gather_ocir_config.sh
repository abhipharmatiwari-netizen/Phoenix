#!/bin/sh
# Gather OCIR/deployment config needed for GitHub Actions secrets.
set -eu

echo "=== phoenix-deploy.env (non-secret values) ==="
grep -vE "PASSWORD|SECRET|TOKEN|KEY|AUTH" /opt/phoenix/phoenix-deploy.env | head -20

echo
echo "=== Running image digests ==="
docker inspect phoenix-oci-backend --format "Image={{.Image}}" 2>/dev/null
docker inspect phoenix-oci-web --format "Image={{.Image}}" 2>/dev/null

echo
echo "=== OCIR namespace from image name ==="
docker inspect phoenix-oci-backend --format "{{.Config.Image}}" 2>/dev/null

echo
echo "=== OCI CLI config (if present) ==="
cat /home/opc/.oci/config 2>/dev/null | grep -vE "key_file|fingerprint" | head -10 || echo "(no OCI CLI config)"

echo
echo "=== SSH public key for CI deploy ==="
cat /home/opc/.ssh/authorized_keys 2>/dev/null | head -3 || echo "(no authorized_keys)"

echo
echo "=== VM instance metadata ==="
curl -s http://169.254.169.254/opc/v1/instance/ 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print('Region:', d.get('regionInfo',{}).get('regionIdentifier','?')); print('OCID:', d.get('id','?')[:40]+'...')" 2>/dev/null || echo "(metadata unavailable)"
