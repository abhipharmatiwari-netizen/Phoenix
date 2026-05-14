#!/bin/bash
# install-scheduler.sh — install Phoenix start/stop/verify scheduler on the VM.
#
# Idempotent: re-running diffs against /etc/systemd/system and only restarts
# the daemon if files actually changed.
#
# Usage (from the deployed app dir, /opt/phoenix/app):
#   sudo bash scripts/install-scheduler.sh           # systemd (preferred)
#   sudo bash scripts/install-scheduler.sh --cron    # fall back to /etc/cron.d
#   sudo bash scripts/install-scheduler.sh --preflight-only
#
# Preflight checks block install if any prerequisite is missing, so a
# misconfigured VM cannot end up with an "enabled but broken" 9 AM timer.

set -euo pipefail

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
SYSTEMD_SRC="${REPO_DIR}/scripts/systemd"
CRON_SRC="${REPO_DIR}/scripts/phoenix.cron"

START_SH="/opt/phoenix/start-phoenix.sh"
STOP_SH="/opt/phoenix/stop-phoenix.sh"
VERIFY_SH="/opt/phoenix/verify-morning-start.sh"
LOG_DIR="/opt/phoenix/logs"
HOLIDAYS_FILE="/opt/phoenix/nse-holidays.txt"
COMPOSE_FILE="/opt/phoenix/app/docker-compose.oci-live.yml"
OVERRIDE_FILE="/opt/phoenix/phoenix-override.yml"
ENV_FILE="/opt/phoenix/phoenix-deploy.env"
SECRET_PG_PASSWORD="/run/secrets/control_plane_pg_password"

MODE="systemd"
PREFLIGHT_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --cron)            MODE="cron" ;;
        --systemd)         MODE="systemd" ;;
        --preflight-only)  PREFLIGHT_ONLY=1 ;;
        -h|--help)
            sed -n '2,12p' "$0"
            exit 0
            ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root (use sudo)" >&2
    exit 1
fi

errors=0
warns=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; warns=$((warns+1)); }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; errors=$((errors+1)); }

echo "== Preflight checks =="

# 1. Required tools — these are the silent-failure cases under cron's minimal PATH.
for cmd in docker python3 systemctl; do
    if command -v "$cmd" >/dev/null 2>&1; then
        ok "$cmd present at $(command -v "$cmd")"
    else
        if [ "$cmd" = "systemctl" ] && [ "$MODE" = "cron" ]; then
            warn "systemctl not found (acceptable with --cron)"
        else
            fail "$cmd not found in PATH"
        fi
    fi
done

# docker compose v2 plugin (preferred) or docker-compose v1 must work.
if docker compose version >/dev/null 2>&1; then
    ok "docker compose v2 plugin available"
elif command -v docker-compose >/dev/null 2>&1; then
    warn "docker compose v2 plugin missing; legacy docker-compose v1 will be used"
else
    fail "neither 'docker compose' nor 'docker-compose' is available"
fi

# 2. Scripts in their canonical /opt/phoenix locations.
for f in "$START_SH" "$STOP_SH"; do
    if [ -x "$f" ]; then ok "executable: $f"
    else                  fail "missing or not executable: $f"
    fi
done

# 3. Compose / env files used by the scripts.
for f in "$COMPOSE_FILE" "$OVERRIDE_FILE" "$ENV_FILE"; do
    if [ -f "$f" ]; then ok "present: $f"
    else                 fail "missing: $f"
    fi
done

# 4. Secret used by backend at runtime (not the host env var, the on-disk secret).
if [ -s "$SECRET_PG_PASSWORD" ]; then
    ok "secret present and non-empty: $SECRET_PG_PASSWORD"
else
    fail "secret missing or empty: $SECRET_PG_PASSWORD"
fi

# 5. Log dir must exist and be writable — silent log failures hide everything else.
if [ -d "$LOG_DIR" ] && [ -w "$LOG_DIR" ]; then
    ok "log dir writable: $LOG_DIR"
else
    fail "log dir missing or not writable: $LOG_DIR"
fi

# 6. Holidays file (warn-only — script tolerates absence, but absence at the
# annual rollover is a known footgun).
if [ -f "$HOLIDAYS_FILE" ]; then
    ok "holidays file present: $HOLIDAYS_FILE"
else
    warn "holidays file missing: $HOLIDAYS_FILE (script will treat every weekday as a trading day)"
fi

# 7. Docker daemon reachable as root (cron/systemd run as root).
if docker info >/dev/null 2>&1; then
    ok "docker daemon reachable"
else
    fail "docker info failed — daemon not reachable as root"
fi

# 8. systemd version supports OnCalendar TZ suffix (>= 247).
if [ "$MODE" = "systemd" ] && command -v systemctl >/dev/null 2>&1; then
    sd_ver=$(systemctl --version | awk '/^systemd/ {print $2; exit}')
    if [ -n "$sd_ver" ] && [ "$sd_ver" -ge 247 ] 2>/dev/null; then
        ok "systemd version $sd_ver supports OnCalendar UTC suffix"
    else
        fail "systemd version $sd_ver too old; need >= 247 for OnCalendar timezone support — use --cron"
    fi
fi

echo
if [ "$errors" -gt 0 ]; then
    echo "Preflight: $errors error(s), $warns warning(s). Aborting install."
    exit 1
fi
echo "Preflight: 0 errors, $warns warning(s)."

if [ "$PREFLIGHT_ONLY" -eq 1 ]; then
    echo "--preflight-only set; not installing."
    exit 0
fi

echo
echo "== Installing verify script =="
install -m 755 -o root -g root "${REPO_DIR}/scripts/verify-morning-start.sh" "$VERIFY_SH"
ok "installed $VERIFY_SH"

if [ "$MODE" = "systemd" ]; then
    echo
    echo "== Installing systemd units =="
    changed=0
    for unit in phoenix-start.service phoenix-start.timer \
                phoenix-stop.service  phoenix-stop.timer  \
                phoenix-verify.service phoenix-verify.timer; do
        src="${SYSTEMD_SRC}/${unit}"
        dst="/etc/systemd/system/${unit}"
        if ! cmp -s "$src" "$dst" 2>/dev/null; then
            install -m 644 -o root -g root "$src" "$dst"
            ok "installed $dst"
            changed=1
        else
            ok "unchanged $dst"
        fi
    done

    if [ "$changed" -eq 1 ]; then
        systemctl daemon-reload
        ok "systemctl daemon-reload"
    fi

    for timer in phoenix-start.timer phoenix-stop.timer phoenix-verify.timer; do
        systemctl enable --now "$timer" >/dev/null
        ok "enabled + started $timer"
    done

    echo
    echo "== Verification =="
    systemctl list-timers 'phoenix-*' --all
    echo
    echo "Next scheduled runs (UTC) shown above. Tail logs with:"
    echo "  journalctl -u phoenix-start.service -u phoenix-stop.service -u phoenix-verify.service -f"
    echo "  tail -f $LOG_DIR/cron-scheduler.log"
else
    echo
    echo "== Installing /etc/cron.d/phoenix =="
    install -m 644 -o root -g root "$CRON_SRC" /etc/cron.d/phoenix
    ok "installed /etc/cron.d/phoenix"

    if systemctl is-active --quiet cron 2>/dev/null; then
        systemctl reload cron 2>/dev/null || systemctl restart cron
        ok "reloaded cron"
    elif systemctl is-active --quiet crond 2>/dev/null; then
        systemctl reload crond 2>/dev/null || systemctl restart crond
        ok "reloaded crond"
    else
        warn "cron service not detected via systemctl — reload it manually"
    fi

    echo
    echo "== Verification =="
    echo "Crontab entries:"
    cat /etc/cron.d/phoenix
    echo
    echo "Tail logs with: tail -f $LOG_DIR/cron-scheduler.log"
fi

echo
echo "Done."
