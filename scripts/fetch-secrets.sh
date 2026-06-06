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
#   admin_kill_switch_override → /run/secrets/admin_kill_switch_override only
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
  # OCI Vault secret names use hyphens; local file names use underscores.
  # e.g. local file: admin_api_key  →  vault name: phoenix-admin-api-key
  local oci_secret_name="phoenix-$(echo "$secret_name" | tr '_' '-')"
  local out_path="$SECRETS_DIR/$secret_name"
  local tmp_path="${out_path}.tmp"

  echo "Fetching secret: $oci_secret_name -> $out_path"

  # Fetch to a temp file so a vault error never truncates the live secret file.
  if ! "$OCI_CLI_BIN" secrets secret-bundle get-secret-bundle-by-name \
    --secret-name "$oci_secret_name" \
    --vault-id "$OCI_VAULT_ID" \
    --auth instance_principal \
    --query 'data."secret-bundle-content".content' \
    --raw-output \
    | base64 -d > "$tmp_path"; then
    rm -f "$tmp_path"
    echo "ERROR: Failed to fetch $oci_secret_name — existing file left unchanged." >&2
    return 1
  fi

  # Reject empty output — a 0-byte secret is never valid.
  if [ ! -s "$tmp_path" ]; then
    rm -f "$tmp_path"
    echo "ERROR: $oci_secret_name returned empty content — existing file left unchanged." >&2
    return 1
  fi

  mv "$tmp_path" "$out_path"
  # Backend runs as appuser inside the container. Keep secrets readable by that
  # non-root process while preventing host world-read exposure.
  chown "${PHOENIX_SECRET_UID:-100}:${PHOENIX_SECRET_GID:-101}" "$out_path"
  chmod 400 "$out_path"
}

fetch_secret "admin_api_key"
fetch_secret "control_plane_pg_password"
fetch_secret "angel_postback_token"
fetch_secret "dashboard_hmac_secret"
fetch_secret "admin_kill_switch_override"
# 2026-05-12: auth_token_secret was previously stored in the vault under the
# legacy name ``phoenix-auth_token_secret`` (underscore in the second segment),
# which required a special-case fetch helper to bypass the hyphen translation.
# The canonical ``phoenix-auth-token-secret`` (all hyphens) was created and
# back-filled with the same value; the legacy ``phoenix-auth_token_secret``
# is scheduled for vault deletion on 2026-06-11. Use the canonical name so
# the round-trip stays consistent after the legacy entry is purged.
fetch_secret "auth_token_secret"

echo "All secrets written to $SECRETS_DIR"
echo "Verify with: ls -la $SECRETS_DIR"
