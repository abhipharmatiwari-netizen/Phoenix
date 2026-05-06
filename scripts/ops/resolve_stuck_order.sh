#!/bin/sh
# Force-terminal the stuck SUBMITTING outbox entry.
# Safe because: broker_order_id is empty (broker never processed it),
# and the broker logs show IP-blocked responses for this session.
set -eu

echo "=== Before: SUBMITTING entries ==="
docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix -c "SELECT submission_key, status, broker_order_id, created_at FROM order_submission_outbox WHERE status='SUBMITTING'"

echo
echo "=== Force-terminal stuck SUBMITTING entry ==="
docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix -c "
UPDATE order_submission_outbox
SET status = 'TERMINAL_NON_FILL',
    broker_order_id = COALESCE(broker_order_id, 'FORCE_TERMINAL_IP_BLOCKED_' || NOW()::text),
    recovery_action = 'force_terminal_ip_blocked_at_startup',
    last_error = 'Force-terminated: entry stuck in SUBMITTING at restart; broker never acknowledged (IP block at time of submission).',
    updated_at = NOW()
WHERE status = 'SUBMITTING'
  AND broker_order_id IS NULL
  AND created_at < NOW() - INTERVAL '5 minutes'
RETURNING submission_key, status, updated_at;
"

echo
echo "=== After: Outbox status counts ==="
docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix -tAc "SELECT status, count(*) FROM order_submission_outbox GROUP BY status"

echo
echo "=== Restart backend ==="
CONTROL_PLANE_PG_PASSWORD_HOST=dummy \
  docker compose \
    -f /opt/phoenix/app/docker-compose.oci-live.yml \
    -f /opt/phoenix/phoenix-override.yml \
    --env-file /opt/phoenix/phoenix-deploy.env \
    restart backend

echo
echo "=== Wait 90s for startup ==="
sleep 90

echo
echo "=== Final container status ==="
docker ps --filter name=phoenix-oci-backend --format "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}"

echo
echo "=== Readyz response ==="
docker exec phoenix-oci-backend wget -qO- http://localhost:8080/readyz 2>&1 | head -1 || \
  docker inspect phoenix-oci-backend --format "{{.State.Health.Status}}"
