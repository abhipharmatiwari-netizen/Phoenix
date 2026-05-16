#!/bin/sh
# Install the Phoenix conditional backend reload timer on the OCI VM.

set -eu

APP_DIR="${PHOENIX_APP_DIR:-/opt/phoenix/app}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
LOGROTATE_DIR="${LOGROTATE_DIR:-/etc/logrotate.d}"

if [ "$(id -u)" -ne 0 ]; then
    echo "FATAL: run as root so systemd and logrotate links can be installed." >&2
    exit 1
fi

[ -d "$APP_DIR" ] || {
    echo "FATAL: app directory not found: $APP_DIR" >&2
    exit 1
}

install -d /opt/phoenix/state /opt/phoenix/logs
chmod +x "$APP_DIR/scripts/backend-reload-if-needed.sh"

ln -sfn "$APP_DIR/ops/systemd/phoenix-backend-reload.service" \
    "$SYSTEMD_DIR/phoenix-backend-reload.service"
ln -sfn "$APP_DIR/ops/systemd/phoenix-backend-reload.timer" \
    "$SYSTEMD_DIR/phoenix-backend-reload.timer"
ln -sfn "$APP_DIR/ops/logrotate/phoenix-backend-reload" \
    "$LOGROTATE_DIR/phoenix-backend-reload"

systemctl daemon-reload
systemctl enable --now phoenix-backend-reload.timer
systemctl status --no-pager phoenix-backend-reload.timer
