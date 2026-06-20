# OI/ML CE Seller Rollout and Rollback Runbook

Status: promotion and rollback checklist for the intraday NIFTY OI/ML CE seller.
Use this after the shadow sidecar gate in `oi_ml_shadow_sidecar.md` has fresh
market-session evidence.

Current operational state (2026-06-20): the sidecar is dormant, its runner,
snapshotter, restart policy, and backend monitoring are disabled, and no fresh
promotion evidence is accumulating. Promotion is blocked until an explicitly
approved reactivation produces new clean-session evidence.

This runbook does not authorize live orders by itself. `oi_ml_ce_seller` remains
disabled by default in `app/config/strategy_env.yaml`, with `allow_naked=false`,
and must not be added to selector dispatch or live routing until every gate below
has signed evidence.

## Scope

Applies only to the v1 NIFTY weekly bear-call-spread plan:

- intraday only, no overnight carry;
- spread-only live entries, no naked CE selling;
- broker-token-backed contract identity required for every leg;
- all live order mutations routed through the hub order router;
- strict intraday, EOD exit, kill-switch, and break-glass controls stay enabled
  during rollout and rollback.

Do not use NSE page scraping as a production feed, do not synthesize OI, and do
not copy broker credentials, admin keys, session tokens, or secret values into
logs, docs, screenshots, PR comments, or GitHub Actions output.

## Promotion Ladder

Each phase requires an operator-owned evidence record that names the commit,
container image, market dates, and exact config change. Promotion is blocked if
`/readyz` is not 200, startup recovery is active, kill-switch divergence is
present, or the OI/ML shadow ingestion health is degraded during the expected
market window.

| Phase | Maximum authority | Minimum evidence before entering next phase |
|---|---|---|
| Data approval | No order intents | Provider decision, field coverage, retention limits, and one-day quality report. |
| Paper | Simulated orders only | 40 clean sessions, approved trained model artifacts, zero overnight positions, profit factor >= 1.25, max simulated drawdown <= 6%, and every session flat by 15:20 IST. |
| Shadow | Live quotes, virtual orders | 10 clean sessions with broker FULL quote snapshots, IV/Greeks/source freshness, latest validation not `ERROR`, virtual lifecycle/PnL evidence, zero live order path, and daily validation reports. |
| Live A | One spread max | 20 sessions at `max_open_spreads=1`, `allow_naked=false`, strict intraday enabled, no EOD residual, no guard bypass, and explicit review after every incident. |
| Live B | Two spreads max | Only after Live A review approves scaling; keep `allow_naked=false`, strict intraday enabled, and add a new dated approval record. |

Live B is the v1 ceiling. Any naked option-selling, overnight carry, higher
spread count, new underlying, or relaxed exit invariant is a new design review,
not an operator toggle.

## Phase Gates

### Data approval

- Document the selected historical and live option-chain provider.
- Prove expired weekly NIFTY contracts are available for the required lookback.
- Confirm required fields: source timestamp, expiry, strike, option type,
  trading symbol, exchange, broker token, OI, volume, IV or approved enrichment,
  bid, ask, LTP, underlying LTP, and India VIX or join key.
- Attach a sample-session coverage report with >= 95% trading-minute coverage
  and >= 98% completeness for candidate-strike hard fields.
- Attach a reconciliation plan against the broker terminal or vendor reference.
- Block the epic if expired full-chain OI with tradable quote fields cannot be
  proven.

### Paper

- Confirm the strategy remains disabled for live order routing.
- Run paper with the same v1 risk envelope intended for Live A:
  `allow_naked=false`, `product_type=INTRADAY`, `max_open_spreads=1`,
  `max_spread_loss_rupees=5000`, and strict intraday targets including
  `oi_ml_ce_seller`.
- Require 40 complete market sessions.
- Require zero strategy positions after 15:20 IST in every session.
- Require profit factor >= 1.25 after costs and slippage.
- Require max simulated drawdown <= 6% of allocated capital.
- Require no accepted trade without broker-token-backed instrument identity.
- Require no paper session with stale PnL, stale option-chain rows, or unresolved
  quality flags on candidate strikes.
- Require `OI_ML_SHADOW_SCORER=lightgbm` or equivalent trained model artifacts
  with a passed validation report; `constant` scorer output is smoke evidence
  only and cannot satisfy Paper or Shadow gates.

### Shadow

- Run the sidecar with live broker quotes and virtual order intents only.
- Confirm `live_order_path_enabled=false` in backend-local `/health/summary`.
  Public nginx `/health/summary` is redacted and is not sufficient for this
  internal gate.
- Require 10 complete sessions with fresh option-chain rows, shadow intents,
  complete virtual lifecycle rows, realized dry-run PnL, and validation reports
  when the market window is open.
- Confirm `OI_ML_SHADOW_SCORER=lightgbm`, explicit model artifact paths, and a
  passed `OI_ML_SHADOW_MODEL_VALIDATION_REPORT_PATH`.
- Confirm candidate generation blocked any quote missing IV, missing Greeks, a
  source timestamp, or a non-stale source timestamp.
- Confirm the latest validation report for the active underlying/expiry is not
  `ERROR`.
- Confirm every staged intent is a bear-call spread with a long hedge leg and a
  short CE leg.
- Confirm no intent is naked, no leg is missing symbol token, and no stale
  snapshot is used for entry decisions.
- Confirm EOD checks show no shadow lifecycle record that would imply overnight
  exposure if it had been live.
- Confirm every same-session record is terminal `FLAT` by 15:20 IST and includes
  `VIRTUAL_FILLED`, `VIRTUAL_EXITED`, and `FLAT` lifecycle events plus
  `realized_pnl_rupees`.
- Review dashboard, backend, sidecar, and Postgres evidence with secrets
  redacted.

### Live A

- Keep `allow_naked=false`.
- Keep `max_open_spreads=1`.
- Keep strict intraday enabled, with `oi_ml_ce_seller` included in the strict
  intraday target set.
- Keep hub EOD hard stop at 15:20 IST and residual retry/alerting until 15:30
  IST.
- Confirm the order router has the option-sell guard interceptor enabled before
  allowing any entry.
- Confirm daily and weekly strategy loss gates are configured from funded
  account capital, not generic defaults.
- Confirm broker margin is sufficient for one protected spread and that margin
  shortfall rejects entries without disabling exits.
- Confirm kill-switch dry run: SOFT trip blocks entries, exits remain allowed,
  durable state is visible, and the clear path is not attempted until broker
  flat plus cancel-all evidence is captured.
- Confirm break-glass flatten can be issued only through the documented
  step-up-token process in LIVE.
- Run exactly one spread maximum for 20 sessions. Any EOD residual, router
  guard bypass, startup recovery block, stale PnL fail-closed event, or naked
  intent stops the phase.

### Live B

- Enter only after a dated Live A review approves scaling.
- Keep `allow_naked=false`.
- Raise only `max_open_spreads` from 1 to 2; do not change the EOD, strict
  intraday, kill-switch, option-sell guard, or broker-token requirements.
- Run a fresh kill-switch dry run and broker-margin checklist for two spreads.
- Revert to Live A or disabled state on any immediate-disable trigger.

## Immediate Disable Triggers

Disable new entries immediately when any of these occurs:

- `/readyz` is non-200 or reports startup recovery, kill-switch divergence, or
  unresolved position authority.
- Broker terminal, StateStore, lifecycle records, or PnL disagree on open
  strategy exposure.
- A strategy position or shadow lifecycle equivalent remains after 15:20 IST.
- Strict intraday retry is not firing for a residual target position.
- Option-chain hard fields are missing or stale on candidate strikes.
- IV, delta, gamma, theta, vega, or source timestamp is missing on any candidate
  strike.
- Validation reports show unexplained severe provider mismatch or latest
  `ERROR` status for the active underlying/expiry.
- A staged or submitted order is naked, missing hedge-first protection, or lacks
  broker symbol/token identity.
- The option-sell guard rejects because PnL, risk, kill-switch, or data state is
  unavailable in LIVE.
- A strategy SL/TP, EOD, trailing-lock, or break-glass exit is in flight and a
  second exit would duplicate it.
- Daily or weekly strategy loss limit is breached.
- Broker margin, quote session, or order lifecycle persistence is degraded.

SOFT strategy kill switches should block entries but must not block exposure-
reducing exits. A HARD kill switch blocks exits too and requires the kill-switch
runbook's HARD-trip recovery path.

## Rollback

Rollback means stop new OI/ML CE-seller entries while preserving cleanup paths.
Do not disable strict intraday, EOD, broker sync, position lifecycle polling,
kill-switch visibility, trailing-lock cleanup, or break-glass controls.

1. Trip a SOFT strategy-scope kill switch for `oi_ml_ce_seller` or use the
   current approved deployment mechanism to remove it from selector dispatch.
2. Set the strategy config back to `enabled: false` before the next deployment.
   Leave `allow_naked=false`.
3. Keep `oi_ml_ce_seller` in strict intraday target coverage until all related
   broker, StateStore, lifecycle, and PnL records are flat or terminal.
4. Cancel open broker orders through the kill-switch runbook if a durable trip
   is active, then verify the broker terminal is flat.
5. Use normal strategy exits, hub EOD, or break-glass flatten for residual
   exposure according to the current runbooks. Do not manually clear lifecycle
   records until broker-flat and authority evidence is captured.
6. Capture backend-local `/readyz`, backend-local `/health/summary`,
   authenticated `/admin/health/summary`, broker terminal flat evidence,
   lifecycle terminal evidence, and audit entries for the rollback record.
7. Keep shadow ingestion running only if it remains dry-run and useful for
   diagnosis. Stop it if quote-provider instability is contributing to incident
   noise.

Rollback is complete only when `/readyz` is 200, broker positions are flat, no
live OI/ML CE-seller order is open or pending, lifecycle records are terminal or
approved-cleared, and the deployment config cannot admit new entries.

## Manual Verification Checklist

Complete this checklist before every phase promotion and after every rollback:

- Broker margin: verify available margin for the phase's maximum spreads and
  record the broker terminal timestamp. Do not paste account numbers or secrets.
- Dashboard route: open the operations dashboard through the current OCI nginx
  route and confirm health, readiness, strategy status, kill switch, positions,
  orders, and OI/ML shadow ingestion panes are visible.
- EOD startup snapshot: capture sanitized backend-local `/readyz`,
  backend-local `/health/summary`, and authenticated `/admin/health/summary`
  output after the backend has started and after EOD on the same session.
- Kill-switch dry run: in paper, shadow, or the approved pre-live test window,
  prove SOFT trip blocks entries, leaves exits allowed, emits durable audit, and
  can be cleared only through the documented clear process.
- Break-glass flatten drill: confirm the operator can obtain a LIVE step-up
  token through the documented ceremony and can identify the exact contract
  fields needed for `POST /admin/break-glass/flatten`. Do not submit a live
  flatten unless real exposure requires it.
- Data gate: verify fresh `option_chain_1m` rows, validation reports, and
  shadow intents for the active listed NIFTY expiry.
- EOD gate: verify no target strategy position is open after 15:20 IST and no
  retry alert remains unresolved after 15:30 IST.
- Secrets hygiene: confirm evidence contains only variable names, record ids,
  counts, timestamps, and redacted payloads.

## Related

- [OI/ML Shadow Sidecar](oi_ml_shadow_sidecar.md)
- [OCI LIVE Deployment](oci_live_deployment.md)
- [Kill Switch](kill_switch.md)
- [Break-Glass Flatten](break_glass_flatten.md)
- [Strategy Runtime Diagnostics](strategy_runtime_diagnostics.md)
- [Position Authority Recovery](position_authority_recovery.md)
