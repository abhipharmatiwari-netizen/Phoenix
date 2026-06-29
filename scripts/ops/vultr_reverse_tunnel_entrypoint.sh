#!/usr/bin/env sh
set -eu

VULTR_TUNNEL_HOST="${VULTR_TUNNEL_HOST:-65.20.69.50}"
VULTR_TUNNEL_USER="${VULTR_TUNNEL_USER:-phoenixproxy}"
VULTR_TUNNEL_REMOTE_BIND="${VULTR_TUNNEL_REMOTE_BIND:-127.0.0.1}"
VULTR_TUNNEL_REMOTE_PORT="${VULTR_TUNNEL_REMOTE_PORT:-18080}"
PHOENIX_TUNNEL_TARGET_HOST="${PHOENIX_TUNNEL_TARGET_HOST:-nginx}"
PHOENIX_TUNNEL_TARGET_PORT="${PHOENIX_TUNNEL_TARGET_PORT:-80}"
PHOENIX_TUNNEL_LIVENESS_URL="${PHOENIX_TUNNEL_LIVENESS_URL:-${PHOENIX_TUNNEL_READY_URL:-http://nginx/nginx-health}}"
VULTR_TUNNEL_RESTART_DELAY_SECONDS="${VULTR_TUNNEL_RESTART_DELAY_SECONDS:-5}"
VULTR_TUNNEL_SSH_KEY_PATH="${VULTR_TUNNEL_SSH_KEY_PATH:-/run/secrets/vultr_reverse_tunnel_ssh_key}"

if [ ! -s "$VULTR_TUNNEL_SSH_KEY_PATH" ]; then
  echo "$(date -Iseconds) SSH key missing or empty: $VULTR_TUNNEL_SSH_KEY_PATH" >&2
  exit 1
fi

mkdir -p /tmp/phoenix-vultr-tunnel
chmod 0700 /tmp/phoenix-vultr-tunnel
cp "$VULTR_TUNNEL_SSH_KEY_PATH" /tmp/phoenix-vultr-tunnel/id_ed25519
chmod 0600 /tmp/phoenix-vultr-tunnel/id_ed25519

while true; do
  if ! curl -fsS --max-time 5 "$PHOENIX_TUNNEL_LIVENESS_URL" >/dev/null; then
    echo "$(date -Iseconds) Phoenix liveness check failed at $PHOENIX_TUNNEL_LIVENESS_URL; retrying in ${VULTR_TUNNEL_RESTART_DELAY_SECONDS}s."
    sleep "$VULTR_TUNNEL_RESTART_DELAY_SECONDS"
    continue
  fi

  echo "$(date -Iseconds) Starting reverse tunnel ${VULTR_TUNNEL_USER}@${VULTR_TUNNEL_HOST}:${VULTR_TUNNEL_REMOTE_BIND}:${VULTR_TUNNEL_REMOTE_PORT} -> ${PHOENIX_TUNNEL_TARGET_HOST}:${PHOENIX_TUNNEL_TARGET_PORT}"
  ssh \
    -i /tmp/phoenix-vultr-tunnel/id_ed25519 \
    -N \
    -T \
    -o ExitOnForwardFailure=yes \
    -o BatchMode=yes \
    -o IdentitiesOnly=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile=/tmp/phoenix-vultr-tunnel/known_hosts \
    -R "${VULTR_TUNNEL_REMOTE_BIND}:${VULTR_TUNNEL_REMOTE_PORT}:${PHOENIX_TUNNEL_TARGET_HOST}:${PHOENIX_TUNNEL_TARGET_PORT}" \
    "${VULTR_TUNNEL_USER}@${VULTR_TUNNEL_HOST}" || status="$?"

  echo "$(date -Iseconds) Reverse tunnel exited with status ${status:-0}; restarting in ${VULTR_TUNNEL_RESTART_DELAY_SECONDS}s."
  status=0
  sleep "$VULTR_TUNNEL_RESTART_DELAY_SECONDS"
done
