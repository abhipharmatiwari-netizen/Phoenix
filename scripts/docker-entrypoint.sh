#!/bin/sh
# Load Docker secrets into environment variables before the main process starts.
# Each file under /run/secrets/ becomes an env var named after the file
# (upper-cased, hyphens -> underscores).  Values are never written to
# docker inspect output because this script runs inside the container.
set -e

SECRETS_DIR="${SECRETS_DIR:-/run/secrets}"
if [ -d "$SECRETS_DIR" ]; then
    for f in "$SECRETS_DIR"/*; do
        [ -f "$f" ] || continue
        varname=$(basename "$f" | tr '[:lower:]' '[:upper:]' | tr '-' '_')
        val=$(cat "$f")
        if [ -n "$val" ]; then
            export "$varname=$val"
        fi
    done
fi

exec "$@"
