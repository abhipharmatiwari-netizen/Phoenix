# Capital Limits Configuration

Phoenix enforces per-order notional and per-account gross-exposure limits via the `CapitalEngine`.
All limits can be tuned per-tenant, per-account, or globally through the `CAPITAL_LIMITS_JSON`
environment variable without touching code.

---

## Hardcoded defaults (production baseline)

| Setting | Default | Compose override |
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

## How to update in production

Set `CAPITAL_LIMITS_JSON` in the PowerShell session before running
`start-docker-secretstore.cmd`, or store it as a SecretStore secret named
`CAPITAL_LIMITS_JSON` so the launch script picks it up automatically:

```powershell
$env:CAPITAL_LIMITS_JSON = '{"default": {"max_notional_per_order": 500000}}'
```

Or via SecretStore:
```powershell
Set-Secret -Name "CAPITAL_LIMITS_JSON" -Secret '{"default": {"max_notional_per_order": 500000}}'
```

The compose file will then substitute the value at container start time.

---

## Source references

- `app/risk/capital_engine.py` — `CapitalEngine`, `CapitalConfig`, `_lookup_env_limits`
- `app/config/settings.py` — `capital_margin_*` fields
- `app/core/feature_flags.py` — `capital_margin_check_mode` LIVE auto-enforce gate
- `docker-compose.live.single.yml` — production env anchor `x-live-backend-env`
