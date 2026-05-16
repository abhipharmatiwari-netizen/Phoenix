#!/bin/sh
# Install the Phoenix optimizer systemd timer on the OCI VM.

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

install -d /opt/phoenix/state /opt/phoenix/logs /opt/phoenix/optimizer/output
chmod +x "$APP_DIR/scripts/optimizer-precheck.sh"

ln -sfn "$APP_DIR/ops/systemd/phoenix-optimizer.service" \
    "$SYSTEMD_DIR/phoenix-optimizer.service"
ln -sfn "$APP_DIR/ops/systemd/phoenix-optimizer.timer" \
    "$SYSTEMD_DIR/phoenix-optimizer.timer"
ln -sfn "$APP_DIR/ops/logrotate/phoenix-optimizer" \
    "$LOGROTATE_DIR/phoenix-optimizer"

systemctl daemon-reload
systemctl enable --now phoenix-optimizer.timer
systemctl status --no-pager phoenix-optimizer.timer
