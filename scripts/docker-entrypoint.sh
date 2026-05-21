#!/bin/sh
# Load Docker secrets into environment variables before the main process starts.
#
# Each file under /run/secrets/ becomes an env var named after the file
# (upper-cased, hyphens → underscores), except secrets that are explicitly
# marked file-only below.
#
# Security note: secrets are exported as env vars so that pydantic-settings and
# os.getenv() callers throughout the app can read them without file I/O at every
# call site. This is a known trade-off: the values become visible in
# /proc/<pid>/environ inside the container. To limit exposure:
#   - The container runs as non-root (appuser), so /proc/<pid>/environ is not
#     world-readable by default.
#   - No crash reporter or log sink that captures full env is configured.
#   - Secrets never appear in `docker inspect` because they are set here at
#     runtime, not in the compose environment block.
#
# If a secret file is empty or missing, the corresponding env var is not set,
# which causes the app to fail at startup (fail-closed behaviour).
set -e

SECRETS_DIR="${SECRETS_DIR:-/run/secrets}"
if [ -d "$SECRETS_DIR" ]; then
    for f in "$SECRETS_DIR"/*; do
        [ -f "$f" ] || continue
        varname=$(basename "$f" | tr '[:lower:]' '[:upper:]' | tr '-' '_')
        case "$varname" in
            ADMIN_KILL_SWITCH_OVERRIDE)
                # File-only: the backend reads /run/secrets/admin_kill_switch_override
                # directly so this break-glass password is not copied into
                # the process environment.
                continue
                ;;
        esac
        val=$(cat "$f")
        if [ -n "$val" ]; then
            export "$varname=$val"
        fi
    done
fi

exec "$@"
