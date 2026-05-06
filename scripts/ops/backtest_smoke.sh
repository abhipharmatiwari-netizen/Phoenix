#!/bin/sh
# Smoke replay runner — validates the path before launching a real sweep.
# Order routing is short-circuited inside replay (isolated_replay_flag).
set -eu

# 1. Pull latest code on host so the container sees the new optimizer grids.
echo "=== git pull on /opt/phoenix/app (as root — repo owned by cron) ==="
sudo git config --global --add safe.directory /opt/phoenix/app 2>/dev/null || true
sudo git -C /opt/phoenix/app pull origin main 2>&1 | tail -5

# 2. Read DB password from the running backend's secret mount.
PG_PASS=$(sudo cat /run/secrets/control_plane_pg_password 2>/dev/null || true)
if [ -z "${PG_PASS:-}" ]; then
    echo "ERROR: cannot read /run/secrets/control_plane_pg_password" >&2
    exit 1
fi
echo "=== PG password loaded (length=${#PG_PASS}) ==="

# 3. Find the docker network the postgres container is on.
PG_NET=$(docker inspect phoenix-oci-postgres --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
echo "=== Postgres network: $PG_NET ==="

DSN="postgresql://phoenix_app:${PG_PASS}@phoenix-oci-postgres:5432/phoenix"

# 4. Probe indicator_bars coverage with a one-shot Python script written into the container.
echo
echo "=== Probing indicator_bars ==="
docker run --rm \
    --network "$PG_NET" \
    -v /opt/phoenix/app:/app:ro \
    -w /tmp \
    -e PYTHONUNBUFFERED=1 \
    -e DSN="$DSN" \
    python:3.12-slim \
    sh -c "pip install --quiet 'psycopg[binary]==3.3.2' && python -c \"
import os, psycopg
with psycopg.connect(os.environ['DSN']) as c:
    with c.cursor() as cur:
        cur.execute('SELECT count(*), min(ts_start)::date, max(ts_start)::date FROM indicator_bars')
        n, mn, mx = cur.fetchone()
        print(f'indicator_bars rows={n} min={mn} max={mx}')
        cur.execute('SELECT symbol, timeframe_seconds, count(*) FROM indicator_bars GROUP BY 1,2 ORDER BY 1,2 LIMIT 30')
        print('symbol | tf | bars')
        for r in cur.fetchall():
            print(' | '.join(str(x) for x in r))
\""

# 5. Verify replay imports work in the container (with full deps).
echo
echo "=== Verifying replay imports ==="
docker run --rm \
    -v /opt/phoenix/app:/app:ro \
    -w /app \
    python:3.12-slim \
    sh -c "pip install --quiet 'psycopg[binary]==3.3.2' pyyaml pandas numpy && \
           python -c 'from scripts.replay.optimizer_runtime import EMA20_GRIDS, extended_ema20_grid; print(\"PHX#185 tp_pct sweep:\", EMA20_GRIDS[\"default\"][\"tp_pct\"])'" 2>&1 | tail -15
