# OI/ML Option-Chain Data Source Approval

This record implements the Phase 0 approval gate for `oi_ml_ce_seller`.
It approves the data-source shape and the automated quality gate; it does not
enable live orders by itself.

## Decision

- Primary live snapshot source: Angel One FULL quote over the broker session
  already used by Phoenix.
- Historical source: an approved vendor export/backfill loaded through
  `scripts/data/backfill_option_chain_1m.py` into `public.option_chain_1m`.
- Validation source: NSE web data is validation-only. It is not a production
  feed and must not drive order routing.
- Retention requirement: at least 18 months of expired weekly NIFTY chains,
  including broker token identity for every tradable contract.
- Stress-window decision: extend backfill beyond 18 months for June 4, 2024 and
  March 2020 windows when the approved vendor can supply those sessions.
- Prohibited: synthetic OI, NSE page scraping as a production source, and
  secret-bearing logs or screenshots.

## Required Fields

Every candidate-strike report must prove these fields: source timestamp, expiry,
strike, option type, trading symbol, exchange, broker token, OI, volume, IV,
delta, gamma, theta, vega, bid, ask, LTP, underlying LTP, and India VIX or an
audited VIX join value. Candidate generation must fail closed when any
candidate-strike source timestamp is missing/stale or any IV/Greek field is
missing.

## Quality Report

Run the one-day report against an imported sample session:

```bash
python scripts/data/report_option_chain_quality.py \
  --input artifacts/oi_ml_sample_session.jsonl \
  --provider angel \
  --underlying NIFTY \
  --session-date 2026-05-19 \
  --candidate-strike 25200 \
  --candidate-strike 25300 \
  --expired-weeklies-available \
  --reconciliation-plan "Compare five random candidate strikes against broker terminal screenshots with secrets cropped" \
  --reconciliation-plan "Compare aggregate OI/volume against independent vendor reference" \
  --output artifacts/oi_ml_data_quality_report.json
```

Passing thresholds:

- trading-minute coverage >= 95 percent for the 09:15-15:30 IST capture window
- candidate-strike hard-field completeness >= 98 percent
- provider retention >= 18 months
- expired weekly NIFTY contracts available from the approved historical source
- reconciliation plan documented
- latest validation report for the active underlying/expiry is not `ERROR`

The report exits with status `4` when any gate fails. A failed report blocks
paper, shadow promotion, and live enablement.

## Evidence Handling

Store only sanitized report JSON and command output. Do not store broker
session tokens, API keys, raw cookies, Authorization headers, or screenshots
containing account identifiers.
