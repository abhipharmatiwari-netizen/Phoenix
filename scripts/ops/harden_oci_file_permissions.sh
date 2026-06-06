#!/bin/sh
# Harden OCI LIVE file permissions without printing file contents.

set -eu

PHOENIX_ROOT="${PHOENIX_ROOT:-/opt/phoenix}"
SECRETS_DIR="${SECRETS_DIR:-/run/secrets}"
SECRET_OWNER="${PHOENIX_SECRET_UID:-100}:${PHOENIX_SECRET_GID:-101}"

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root so ownership changes are applied consistently" >&2
  exit 1
fi

if [ -d "$SECRETS_DIR" ]; then
  find "$SECRETS_DIR" -maxdepth 1 -type f -print | while IFS= read -r path; do
    chown "$SECRET_OWNER" "$path"
    chmod 400 "$path"
  done
fi

if [ -f "$PHOENIX_ROOT/phoenix-deploy.env" ]; then
  chmod 600 "$PHOENIX_ROOT/phoenix-deploy.env"
fi

find "$PHOENIX_ROOT" -maxdepth 1 -type f \
  \( -name 'phoenix-deploy.env.*' -o -name 'phoenix-deploy.env.bak*' \) \
  -print | while IFS= read -r path; do
    chmod 600 "$path"
  done

"$(dirname "$0")/../validate-live-secret-perms.sh"
echo "OCI Phoenix file permissions hardened"
