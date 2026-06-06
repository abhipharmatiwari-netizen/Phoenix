#!/bin/sh
# Validate LIVE secret file ownership/mode without printing secret values.

set -eu

SECRETS_DIR="${SECRETS_DIR:-/run/secrets}"
EXPECTED_OWNER="${PHOENIX_SECRET_UID:-100}:${PHOENIX_SECRET_GID:-101}"

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
  if [ "$mode" != "400" ]; then
    echo "ERROR: $name has mode $mode, expected 400" >&2
    fail=1
  fi
  if [ "$owner" != "$EXPECTED_OWNER" ]; then
    echo "ERROR: $name owner $owner, expected $EXPECTED_OWNER" >&2
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  exit "$fail"
fi

echo "LIVE secret permissions OK"
