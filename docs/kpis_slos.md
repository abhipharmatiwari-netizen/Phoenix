# Phoenix v9 — Runtime KPIs and SLO Targets

## Key Performance Indicators

### Market Data Health
| Metric | Description | Emit Location | SLO Target |
|--------|-------------|---------------|------------|
| `feed_heartbeat_age_seconds` | Time since last tick received per token label | `ws_runner.py` | < 5s during market hours |
| `feed_gap_count` | Number of detected time gaps in candle stream | `indicators_engine.py` | 0 for must-trade symbols |
| `feed_reconnect_count` | WebSocket reconnection events per session | `ws_runner.py` | < 3 per session |
| `feed_backfill_count` | Backfill requests triggered by gap detection | `indicators_engine.py` | Matches gap_count |

### Broker Sync
| Metric | Description | Emit Location | SLO Target |
|--------|-------------|---------------|------------|
| `position_sync_staleness_seconds` | Age of last successful position sync per account | `account_runner.py` | < 60s |
| `position_sync_mismatch_count` | Positions differing between internal state and broker | `position_sync.py` | 0 (critical alert) |
| `broker_blocked_or_rate_limited` | Broker API returning 429 or blocking | `angel_client.py` | 0 (warning at 1+) |

### Order Pipeline
| Metric | Description | Emit Location | SLO Target |
|--------|-------------|---------------|------------|
| `order_submit_latency_ms` | Time from intent creation to broker ack | `order_client.py` | p99 < 2000ms |
| `order_reject_rate` | Broker order rejections / total submissions | `order_lifecycle.py` | < 1% over 5min window |
| `order_duplicate_suppressed_count` | Idempotency-blocked duplicate submissions | `router.py` | Informational (should be 0 normally) |
| `order_outbox_pending_count` | Outbox entries not yet confirmed | `router.py` | < 5 (alert at 10+) |

### Risk and Policy
| Metric | Description | Emit Location | SLO Target |
|--------|-------------|---------------|------------|
| `risk_block_count` | Orders blocked by risk interceptor (by reason) | `interceptors.py` | Informational |
| `capital_block_count` | Orders blocked by capital adequacy | `capital_engine.py` | Informational |
| `profit_block_count` | Orders blocked by daily target reached | `profit_engine.py` | Informational |
| `kill_switch_active` | Whether global kill switch is engaged | `interceptors.py` | 0 (critical when 1) |
| `circuit_breaker_state` | Current state of each circuit breaker | `risk_engine.py` | open=0 (alert when tripped) |

### PnL and Profit
| Metric | Description | Emit Location | SLO Target |
|--------|-------------|---------------|------------|
| `realized_pnl_total` | Cumulative realized PnL per account today | `pnl_engine.py` | Informational |
| `open_pnl_total` | Mark-to-market unrealized PnL | `server.py` (dashboard) | Informational |
| `profit_sweep_count` | Number of profit sweeps executed today | `exit_engines.py` | <= max_sweeps_per_day |
| `profit_lock_triggered` | Whether profit lock is currently active | `profit_lock.py` | Informational |

### System Health
| Metric | Description | Emit Location | SLO Target |
|--------|-------------|---------------|------------|
| `stream_worker_running` | Whether the stream worker thread is alive | `app_runtime.py` | 1 during market hours |
| `watchdog_restart_count` | Stream worker restarts by watchdog | `app_runtime.py` | 0 (alert at 2+ per hour) |
| `hub_runner_count` | Active AccountRunners in the hub | `hub.py` | Matches expected tenant count |
| `schema_status` | Database schema check result | `app_runtime.py` | "ok" |

## Alert Severity Levels

| Severity | Response Time | Examples |
|----------|--------------|---------|
| **P1 — Critical** | Immediate (page) | Position mismatch > 0, kill switch active, stream worker dead during market hours |
| **P2 — High** | < 15 min | Feed heartbeat stale > 30s, order reject rate > 5%, broker blocked |
| **P3 — Medium** | < 1 hour | Watchdog restart, schema degraded, outbox backlog > 5 |
| **P4 — Low** | Next business day | Elevated duplicate suppression, risk blocks trending up |

## Instrumentation Notes

- KPI definitions documented in this file; mapped to metric/log fields.
- Prometheus `/metrics` endpoint is active (`app/observability/prometheus_metrics.py`, served at `GET /metrics`).
- OpenTelemetry tracing wired (`app/observability/tracing.py`).
- `GET /health/alerts` is the canonical in-repo day-1 alert surface for runtime, broker, order, and dashboard freshness failures.

## Day 1 Live Monitor Set

The minimum LIVE monitor set for cutover is the set below. This is the monitor story the repo actually supports on day 1.

| Failure mode | Operator signal | Day-1 alert path | Cutover expectation | Owner |
|----------|--------------|---------|------------|-------|
| Backend health | `GET /health`, `GET /health/summary`, container health status | Treat any non-200 response, `ready=false`, `stream_worker_running=false`, or unhealthy summary as a stop-the-line incident | Healthy before cutover, during cutover, and after green takes write authority | `release_commander` plus `platform_on_call` |
| Order rejection / error rate | `GET /health/alerts` rule `high_order_rejection_rate`; confirm with `/metrics` metric `phoenix_orders_total{status="rejected"}` | A firing `high_order_rejection_rate` alert is the day-1 rejection-rate alert | No unexplained firing rejection alert during cutover validation | `trading_on_call` |
| WebSocket / dashboard availability | Browser dashboard over `WS /ws/dashboard`; `GET /health/alerts` rule `dashboard_freshness_lag` | Dashboard disconnect, auth/ticket failure, or freshness lag alert requires operator action | Dashboard must connect and show fresh state during validation | `platform_on_call` |
| Kill switch / circuit breaker state | `GET /health/alerts` rule `circuit_breaker_tripped`; dashboard risk payload field `kill_switch_active`; kill-switch logs and audit trail | Any active kill switch or firing circuit breaker alert is stop-the-line | Must be acknowledged and understood before cutover proceeds | `trading_on_call` |
| Broker/API latency or failure | `GET /health/alerts` rules `position_sync_failures`, `stale_broker_sync`, and `stale_quote_age`; confirm with `/metrics` metrics `phoenix_position_syncs_total`, `phoenix_broker_sync_age_seconds`, and `phoenix_quote_age_seconds` | Any firing broker freshness/failure alert requires hold or rollback decision | Broker sync and market-data freshness must be healthy before automated LIVE continues | `trading_on_call` plus `platform_on_call` |

## Release Posture

- Phoenix v9 does not ship a repo-managed Grafana dashboard bundle or Alertmanager routing config for LIVE.
- LIVE cutover is therefore approved only as supervised operation on day 1 and through the initial soak window.
- Before cutover starts, release evidence must name the assigned `release_commander`, `trading_on_call`, and `platform_on_call`.
- Cutover is blocked if any named owner is missing or any required monitoring surface is unavailable: `/health`, `/health/summary`, `/health/alerts`, `/metrics`, and the authenticated dashboard WebSocket.
- Operators must keep `/health/alerts` and the dashboard open throughout cutover and initial soak; these are the required day-1 alerting surfaces for the minimum live failure modes.
- Soak validation is not complete, so this release must not be represented as unattended SLO/pager-driven operation yet.

## Required Cutover Evidence

- Capture `/health`, `/health/summary`, and `/health/alerts` output for green before cutover and after cutover.
- Capture evidence that the dashboard connected successfully and was receiving fresh updates from `WS /ws/dashboard`.
- Record the names of the `release_commander`, `trading_on_call`, and `platform_on_call` who supervised the cutover.
- If any minimum monitor fired during cutover, record the alert name, timestamp, operator decision, and rollback/continue rationale.
