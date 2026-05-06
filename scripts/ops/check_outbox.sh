#!/bin/sh
set -eu
echo "=== Outbox by status ==="
docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix -tAc "SELECT status, count(*) FROM order_submission_outbox GROUP BY status ORDER BY status"
echo
echo "=== Non-terminal entries ==="
docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix -c "SELECT status, submission_key, broker_order_id, recovery_action, created_at FROM order_submission_outbox WHERE status NOT LIKE 'TERMINAL%'"
echo
echo "=== internal_position_records non-terminal ==="
docker exec phoenix-oci-postgres psql -U phoenix_app -d phoenix -c "SELECT status, count(*) FROM internal_position_records GROUP BY status" 2>/dev/null || echo "(table not found)"
