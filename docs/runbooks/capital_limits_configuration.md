# Capital Limits Configuration

## Purpose

Configure `CAPITAL_LIMITS_JSON` and related margin controls without weakening LIVE fail-closed behavior.

## Scope

This guide applies to the current OCI VM runtime documented in
[`docs/OCI_VM_RUNTIME.md`](../OCI_VM_RUNTIME.md). Docker Desktop examples in old
revisions are non-current for production. This guide does not approve
helper-derived generic limits for funded LIVE accounts, repo-stored secrets, or
disabling capital/risk checks.

## Preconditions

- You know the funded tenant and broker account IDs.
- You have operator approval for the exposure and per-order limits.
- The deployment path injects values from the operator environment or platform secret store, not from a committed env file.

Phoenix enforces per-order notional and per-account gross-exposure limits via the `CapitalEngine`.
All limits can be tuned per-tenant, per-account, or globally through the `CAPITAL_LIMITS_JSON`
environment variable without touching code.

---

## Baseline limits

| Setting | Baseline | Compose override |
|---|---|---|
| `max_notional_per_order` | 5,00,000 (5L) | via `CAPITAL_LIMITS_JSON` |
| `max_gross_exposure` | 10,00,000 (10L) | via `CAPITAL_LIMITS_JSON` |
| `CAPITAL_MARGIN_SHORT_OPTION_PER_LOT` | 2,00,000 (2L) | env var directly |
| `CAPITAL_MARGIN_FUTURES_PER_LOT` | 2,00,000 (2L) | env var directly |
| `CAPITAL_MARGIN_FUTURES_RATE` | 0.12 (12%) | env var directly |
| `CAPITAL_MARGIN_CHECK_MODE` | `enforce` (LIVE) | env var directly |

---

## CAPITAL_LIMITS_JSON override format

`CAPITAL_LIMITS_JSON` is a JSON object. Keys are matched against each order in priority order:

```
"tenant_id:broker_account_id"  →  most specific
"broker_account_id"
"tenant_id"
"default"
"*"                            →  least specific (wildcard)
```

Each value is an object with any subset of the following fields:

```jsonc
{
  "max_notional_per_order": 500000,   // null = unbounded
  "max_gross_exposure": 1000000       // null = unbounded
}
```

### Examples

**Single global override (all accounts):**
```json
{"default": {"max_notional_per_order": 300000}}
```

**Per-account override:**
```json
{
  "tenant-1:A1": {"max_notional_per_order": 500000, "max_gross_exposure": 1000000},
  "tenant-1:A2": {"max_notional_per_order": 200000}
}
```

**Disable notional cap for one account, keep others at 3L:**
```json
{
  "tenant-1:A1": {"max_notional_per_order": null},
  "default":     {"max_notional_per_order": 300000}
}
```

**Wildcard fallback:**
```json
{"*": {"max_notional_per_order": 500000}}
```

---

## Margin mode settings

`CAPITAL_MARGIN_CHECK_MODE` controls how short-option and futures margin requirements are enforced:

| Mode | Behaviour |
|---|---|
| `off` | No margin check (auto-upgraded to `enforce` when `TRADE_MODE=LIVE`) |
| `shadow` | Logs margin breach via `CAPITAL_MARGIN_SHADOW` event; does **not** block the order |
| `enforce` | Blocks the order when estimated margin > available balance |

Per-lot margin estimates are configured via:

| Env var | Applies to |
|---|---|
| `CAPITAL_MARGIN_SHORT_OPTION_PER_LOT` | Short option SELL orders (CE/PE) |
| `CAPITAL_MARGIN_FUTURES_PER_LOT` | Futures orders — floor per lot |
| `CAPITAL_MARGIN_FUTURES_RATE` | Futures orders — fraction of notional |

Futures required margin = `max(notional × rate, lots × per_lot)`.

---

## How to update

For funded LIVE accounts, prefer a specific `tenant_id:broker_account_id` key. Set
`CAPITAL_LIMITS_JSON` in the operator environment before deployment, or store it
in the approved platform secret process for your deployment path.

Current OCI VM example:

```bash
docker exec phoenix-oci-backend sh -lc 'test -n "$CAPITAL_LIMITS_JSON" && echo CAPITAL_LIMITS_JSON_PRESENT'
docker exec phoenix-oci-backend curl -sS http://localhost:8080/readyz
```

Do not print the JSON value into tickets or docs.

Non-current local example:

```powershell
$env:CAPITAL_LIMITS_JSON = '{"tenant-1:A1": {"max_notional_per_order": 500000, "max_gross_exposure": 1000000}}'
```

Or via SecretStore:
```powershell
Set-Secret -Name "CAPITAL_LIMITS_JSON" -Secret '{"tenant-1:A1": {"max_notional_per_order": 500000, "max_gross_exposure": 1000000}}'
```

OCI Compose example: set `CAPITAL_LIMITS_JSON` in `/opt/phoenix/phoenix-deploy.env`
from the operator secret store before running the OCI deployment runbook.

The compose file requires this value at container start time. Empty `{}` is
rejected in `TRADE_MODE=LIVE` unless `ALLOW_LIVE_CAPITAL_LIMITS_DEFAULT_ONLY=true`
is deliberately set as an audited exception. Do not use the helper-derived
5L/10L fallback as funded-account go-live evidence.

## Validation

```powershell
docker compose -f .\docker-compose.live.single.yml exec backend printenv CAPITAL_LIMITS_JSON
curl.exe http://localhost/readyz
```

Expected success evidence:

- `CAPITAL_LIMITS_JSON` contains the approved funded-account key.
- `/readyz` is healthy.
- Startup logs do not contain `startup.capital_limits_override` unless an audited exception was approved.

## Failure handling and rollback

If startup rejects the limit payload, stop the stack, restore the last approved `CAPITAL_LIMITS_JSON`, redeploy, and capture the failed payload plus startup error in the release record. Do not disable capital checks to recover a LIVE deployment.

---

## Source references

- `app/risk/capital_engine.py` — `CapitalEngine`, `CapitalConfig`, `_lookup_env_limits`
- `app/config/settings.py` — `capital_margin_*` fields
- `app/core/feature_flags.py` — `capital_margin_check_mode` LIVE auto-enforce gate
- `docker-compose.live.single.yml` — production env anchor `x-live-backend-env`
