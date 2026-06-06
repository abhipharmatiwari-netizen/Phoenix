#!/bin/sh
# Validate LIVE secret file ownership/mode without printing secret values.

set -eu

SECRETS_DIR="${SECRETS_DIR:-/run/secrets}"
EXPECTED_OWNER="${PHOENIX_SECRET_UID:-100}:${PHOENIX_SECRET_GID:-101}"
EXPECTED_SHARED_OWNER="${PHOENIX_SECRET_UID:-100}:${PHOENIX_SHARED_SECRET_GID:-0}"

if [ ! -d "$SECRETS_DIR" ]; then
  echo "ERROR: secrets directory not found: $SECRETS_DIR" >&2
  exit 1
fi

fail=0

file_mode() {
  stat -c "%a" "$1" 2>/dev/null || stat -f "%Lp" "$1"
}

file_owner() {
  stat -c "%u:%g" "$1" 2>/dev/null || stat -f "%u:%g" "$1"
}

for path in "$SECRETS_DIR"/*; do
  [ -f "$path" ] || continue
  name=$(basename "$path")
  mode=$(file_mode "$path")
  owner=$(file_owner "$path")
  case "$name" in
    admin_api_key|control_plane_pg_password)
      expected_mode="440"
      expected_owner="$EXPECTED_SHARED_OWNER"
      ;;
    *)
      expected_mode="400"
      expected_owner="$EXPECTED_OWNER"
      ;;
  esac
  if [ "$mode" != "$expected_mode" ]; then
    echo "ERROR: $name has mode $mode, expected $expected_mode" >&2
    fail=1
  fi
  if [ "$owner" != "$expected_owner" ]; then
    echo "ERROR: $name owner $owner, expected $expected_owner" >&2
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  exit "$fail"
fi

echo "LIVE secret permissions OK"
