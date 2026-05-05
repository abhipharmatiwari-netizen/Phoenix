#!/bin/sh
# fetch-secrets.sh — pull secrets from OCI Vault and write to /run/secrets/
#
# Called before `docker compose -f docker-compose.oci-live.yml up` on the
# OCI Compute Instance.  Uses OCI Instance Principal auth (no API keys needed).
#
# Required env vars:
#   OCI_VAULT_ID              OCID of the phoenix-vault (retrieve from ops secrets store)
#
# Optional env vars:
#   SECRETS_DIR               output directory (default: /run/secrets)
#   OCI_CLI_BIN               path to oci CLI (default: oci)
#
# Usage:
#   sudo OCI_VAULT_ID=<VAULT_OCID> ./scripts/fetch-secrets.sh
#
# Secrets written (names match Docker secret file convention: filename → env var):
#   admin_api_key             → ADMIN_API_KEY
#   auth_token_secret         → AUTH_TOKEN_SECRET  (JWT signing; was demo_auth_token_secret)
#   control_plane_pg_password → CONTROL_PLANE_PG_PASSWORD
#   angel_postback_token      → ANGEL_POSTBACK_TOKEN
#   dashboard_hmac_secret     → DASHBOARD_HMAC_SECRET
#
# OCI Vault secret names follow the convention: phoenix-<secret_name>
# e.g. phoenix-auth_token_secret, phoenix-admin_api_key, etc.

set -eu

SECRETS_DIR="${SECRETS_DIR:-/run/secrets}"
OCI_CLI_BIN="${OCI_CLI_BIN:-oci}"

if [ -z "${OCI_VAULT_ID:-}" ]; then
  echo "ERROR: OCI_VAULT_ID is not set. Export the OCID of the phoenix-vault before running." >&2
  exit 1
fi

# Verify OCI CLI is available and instance principal auth works.
if ! command -v "$OCI_CLI_BIN" >/dev/null 2>&1; then
  echo "ERROR: OCI CLI not found at '$OCI_CLI_BIN'. Install it first." >&2
  exit 1
fi

mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

fetch_secret() {
  local secret_name="$1"
  local oci_secret_name="phoenix-${secret_name}"
  local out_path="$SECRETS_DIR/$secret_name"

  echo "Fetching secret: $oci_secret_name -> $out_path"

  "$OCI_CLI_BIN" secrets secret-bundle get-secret-bundle-by-name \
    --secret-name "$oci_secret_name" \
    --vault-id "$OCI_VAULT_ID" \
    --auth instance_principal \
    --query 'data."secret-bundle-content".content' \
    --raw-output \
    | base64 -d > "$out_path"

  chmod 600 "$out_path"
}

fetch_secret "admin_api_key"
fetch_secret "auth_token_secret"
fetch_secret "control_plane_pg_password"
fetch_secret "angel_postback_token"
fetch_secret "dashboard_hmac_secret"

echo "All secrets written to $SECRETS_DIR"
echo "Verify with: ls -la $SECRETS_DIR"
