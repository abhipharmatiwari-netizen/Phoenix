#!/bin/sh
# Detect forbidden secret-like material in OCI deployment env files.

set -eu

PHOENIX_ROOT="${PHOENIX_ROOT:-/opt/phoenix}"
ENV_FILE="${ENV_FILE:-$PHOENIX_ROOT/phoenix-deploy.env}"

fail=0

scan_file() {
  file="$1"
  [ -f "$file" ] || return 0
  line_no=0
  while IFS= read -r raw_line || [ -n "$raw_line" ]; do
    line_no=$((line_no + 1))
    line=$(printf '%s' "$raw_line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    [ -n "$line" ] || continue
    case "$line" in \#*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key=$(printf '%s' "${line%%=*}" | sed 's/[[:space:]]*$//')
    value=$(printf '%s' "${line#*=}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    upper_key=$(printf '%s' "$key" | tr '[:lower:]' '[:upper:]')
    case "$upper_key" in
      *PASSWORD*|*SECRET*|*TOKEN*|*API_KEY*|*PRIVATE_KEY*|*CLIENT_CODE*|*PIN*|*TOTP*)
        echo "ERROR: $(basename "$file") line $line_no has forbidden secret-like key: $key" >&2
        fail=1
        ;;
    esac
    case "$value" in
      *"-----BEGIN"*PRIVATE*KEY*|ghp_*|gho_*|github_pat_*|sk-*|xoxb-*|xoxp-*)
        echo "ERROR: $(basename "$file") line $line_no has forbidden token-like value for key: $key" >&2
        fail=1
        ;;
    esac
  done < "$file"
}

scan_file "$ENV_FILE"

for backup_file in "$PHOENIX_ROOT"/phoenix-deploy.env.* "$PHOENIX_ROOT"/phoenix-deploy.env.bak*; do
  [ -f "$backup_file" ] || continue
  scan_file "$backup_file"
done

if [ "$fail" -ne 0 ]; then
  exit "$fail"
fi

echo "Deployment env secret-material check OK"
