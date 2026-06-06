# Phoenix Runtime KPIs And SLO Targets

## Purpose

Define the day-1 runtime signals that operators can actually observe from the current codebase.

## Scope

This document covers in-repo observability only: `GET /metrics`,
`GET /health/alerts`, backend-local `/readyz`, container health, logs, and the
authenticated dashboard WebSocket. Phoenix does not currently ship a
repo-managed Grafana dashboard, Alertmanager routing config, or unattended
pager policy.

## Current Metric Surfaces

| Surface | Code evidence | Operator use |
|---|---|---|
| `GET /metrics` | `app/server.py`, `app/observability/prometheus_metrics.py` | Prometheus-format counters, gauges, and histograms |
| `GET /health/alerts` | `app/server.py`, `app/observability/alert_rules.py` | In-repo alert evaluation for day-1 supervision |
| `/readyz` | `app/server.py` | Release gate for leader lease, startup recovery, stream worker, sync freshness, kill switch, position authority, and LIVE universe/quote-auth health |
| `/readyz-public` and `/health/summary-public` | `app/server.py`, `nginx/*.conf.template` | Redacted public nginx readiness/summary responses |
| `/admin/release-evidence` | `app/runtime/app_runtime.py`, `app/server.py` | Promotion evidence snapshot |
| Dashboard WebSocket | `app/server.py`, `frontend/` | Derived operator view; not authoritative state |

## Metrics Exported By Current Code

| Metric | Type | Emitted by | Notes |
|---|---|---|---|
| `phoenix_uptime_seconds` | gauge | `render_metrics()` | Process uptime |
| `phoenix_orders_total{tenant_id,broker_account_id,strategy_id,status}` | counter | `record_order_submitted()` | Used by `high_order_rejection_rate` |
| `phoenix_order_latency_seconds` | histogram | `record_order_latency()` | Order submission latency |
| `phoenix_policy_decisions_total{source,action}` | counter | `record_policy_decision()` | Risk/policy decisions |
| `phoenix_circuit_breaker_trips_total{type}` | counter | `record_circuit_breaker_trip()` | Used by `circuit_breaker_tripped` |
| `phoenix_ticks_received_total{underlying}` | counter | `record_tick_received()` | Market data tick count |
| `phoenix_tick_gaps_total{underlying}` | counter | `record_tick_gap_detected()` | Used by `tick_data_gaps` |
| `phoenix_position_syncs_total{broker_account_id,success}` | counter | `record_position_sync()` | Used by `position_sync_failures` |
| `phoenix_realized_pnl{tenant_id,broker_account_id}` | gauge | `record_realized_pnl()` | Used by `pnl_drawdown_critical` |
| `phoenix_signals_checked_total` | gauge | signal metrics bridge | Mirrored from in-memory signal summary |
| `phoenix_signals_fired_total` | gauge | signal metrics bridge | Mirrored from in-memory signal summary |
| `phoenix_orders_submitted_total` | gauge | signal metrics bridge | Mirrored from in-memory signal summary |
| `phoenix_uptime_app_seconds` | gauge | signal metrics bridge | App uptime from signal metrics |

The alert rules also read optional metrics such as `phoenix_quote_age_seconds`, `phoenix_broker_sync_age_seconds`, `phoenix_stuck_orders_count`, `phoenix_reconciliation_backlog_count`, `phoenix_outbox_pending_count`, `phoenix_ownership_conflicts_total`, `phoenix_deadletter_count`, `phoenix_dashboard_lag_seconds`, and `phoenix_lease_renewal_failures_total` when those are populated by runtime components.

## Day-1 Alert Rules

| Rule | Severity | Trigger in code |
|---|---|---|
| `circuit_breaker_tripped` | critical | any `phoenix_circuit_breaker_trips_total` sample |
| `high_order_rejection_rate` | warning | rejection rate > 30% after at least 5 orders |
| `position_sync_failures` | warning | `phoenix_position_syncs_total{success=false}` >= 3 |
| `tick_data_gaps` | warning | total tick gaps >= 5 |
| `pnl_drawdown_critical` | critical | realized PnL below -50000 |
| `stale_quote_age` | critical | max quote age > 120 seconds |
| `stale_broker_sync` | warning | max broker sync age > 180 seconds |
| `stuck_orders` | warning | stuck order count > 0 |
| `reconciliation_backlog` | warning | backlog count > 5 |
| `outbox_replay_backlog` | warning | pending outbox count > 10 |
| `ownership_conflicts` | critical | ownership conflicts > 3 |
| `deadletter_growth` | warning | deadletter count > 0 |
| `dashboard_freshness_lag` | info | dashboard lag > 10 seconds |
| `leader_lease_failure` | critical | leader lease renewal failure > 0 |

## Cutover Expectations

| Failure mode | Required signal | Operator expectation |
|---|---|---|
| Backend readiness | backend-local `/readyz`, container health | Docker health proves liveness; backend-local `/readyz` must be 200 before automated LIVE entries continue |
| Order rejection / error rate | `/health/alerts`, `phoenix_orders_total` | No unexplained firing rejection alert |
| WebSocket / dashboard availability | dashboard WebSocket, `dashboard_freshness_lag` | Dashboard connects and remains fresh |
| Kill switch / circuit breaker state | `/readyz`, `/health/alerts`, kill-switch audit logs | Any active kill switch is stop-the-line until reviewed |
| Broker/API latency or failure | `/health/alerts`, sync fields and universe health in `/readyz` | Broker sync and market-data freshness must be healthy |

## Release Posture

- LIVE cutover is approved only as supervised operation on day 1 and through the initial soak window.
- Before cutover starts, release evidence must name `release_commander`, `trading_on_call`, and `platform_on_call`.
- Cutover is blocked if `/health`, backend-local `/health/summary`,
  `/health/alerts`, `/metrics`, backend-local `/readyz`, or the authenticated
  dashboard WebSocket is unavailable.
- Public nginx `/readyz` and `/health/summary` must remain redacted; they are
  exposure checks, not full diagnostic evidence.
- Soak validation is not complete, so this release must not be represented as unattended SLO/pager-driven operation yet.

## Required Cutover Evidence

- Capture backend-local `/readyz`, `/health`, backend-local `/health/summary`,
  `/health/alerts`, and `/metrics` output for green before and after cutover.
- Capture public nginx `/readyz` only to prove redaction and high-level
  reachability.
- Capture evidence that the dashboard connected successfully and was receiving fresh updates.
- Record the named on-call owners.
- If any minimum monitor fired, record the alert name, timestamp, operator decision, and rollback/continue rationale.
