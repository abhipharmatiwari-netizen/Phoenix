#!/bin/sh
# Gather OCIR/deployment config needed for GitHub Actions secrets.
#
# CAUTION: output may contain semi-sensitive infrastructure identifiers
# (OCI region, instance OCID). Do not forward to public channels or logs.
#
# Usage:
#   sh gather_ocir_config.sh                  # standard output, no SSH keys
#   sh gather_ocir_config.sh --show-ssh-keys  # include authorized_keys (public keys only)
set -eu

SHOW_SSH_KEYS=0
for arg in "$@"; do
  case "$arg" in
    --show-ssh-keys) SHOW_SSH_KEYS=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

echo "=== phoenix-deploy.env (non-secret values) ==="
grep -vE "PASSWORD|SECRET|TOKEN|KEY|AUTH" /opt/phoenix/phoenix-deploy.env | head -20

echo
echo "=== Running image digests ==="
docker inspect phoenix-oci-backend --format "Image={{.Image}}" 2>/dev/null || echo "(phoenix-oci-backend not running)"
docker inspect phoenix-oci-web --format "Image={{.Image}}" 2>/dev/null || echo "(phoenix-oci-web not running)"

echo
echo "=== OCIR namespace from image name ==="
docker inspect phoenix-oci-backend --format "{{.Config.Image}}" 2>/dev/null || echo "(phoenix-oci-backend not running)"

echo
echo "=== OCI CLI config (if present) ==="
cat /home/opc/.oci/config 2>/dev/null | grep -vE "key_file|fingerprint" | head -10 || echo "(no OCI CLI config)"

if [ "$SHOW_SSH_KEYS" = "1" ]; then
  echo
  echo "=== SSH public key for CI deploy (--show-ssh-keys requested) ==="
  cat /home/opc/.ssh/authorized_keys 2>/dev/null | head -3 || echo "(no authorized_keys)"
else
  echo
  echo "=== SSH public key for CI deploy ==="
  echo "(omitted — pass --show-ssh-keys to include)"
fi

echo
echo "=== VM instance metadata ==="
curl -s http://169.254.169.254/opc/v1/instance/ 2>/dev/null | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print('Region:', d.get('regionInfo',{}).get('regionIdentifier','?')); print('OCID:', d.get('id','?')[:40]+'...')" 2>/dev/null || \
  echo "(metadata unavailable)"
