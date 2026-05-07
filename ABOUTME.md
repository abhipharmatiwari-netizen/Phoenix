# About Phoenix v9

Phoenix v9 is an operator-run trading platform. Its production purpose is to execute and supervise real trades under a fail-closed control model.

This file explains Phoenix in plain language. It does **not** replace [`ARCHITECTURE.md`](ARCHITECTURE.md). If there is any conflict, the architecture document is authoritative.

---

## What Phoenix is

In the current recommended automated LIVE runtime, Phoenix is:

- a **hub-authoritative LIVE system**
- a **hybrid runtime** where the stream worker stays enabled for market data and strategy signals, while the hub/router/lifecycle path remains authoritative for broker-facing order control
- a **Postgres-authoritative operational system** for durable trading state
- a **browser-facing console behind nginx**, with the backend kept behind the deployment boundary in the bundled Docker/Desktop path
- a system that **fails closed** when required authority decisions, durable stores, fresh marks, or secret inputs are missing or ambiguous

---

## What Phoenix is not

Phoenix is **not** any of the following in the current recommended automated LIVE contract:

- a legacy-authoritative LIVE deployment
- a stream-disabled automated LIVE deployment without an approved replacement market-data plane
- an env-file-secret deployment
- a "best effort" startup that guesses missing state
- a demo system that allows public registration, public promotion, or disabled auth in LIVE
- a production system that treats BigQuery, Firestore, CSV files, dashboard state, or in-memory fallbacks as authoritative operational storage

The repository may still contain local development assets and compatibility paths. Those are not the automated LIVE baseline.

The repo also contains an OCI Compose deployment surface. It must prove the same runtime contract as Docker/Desktop from the running backend container; it is not automatically approved just because the manifest exists.

---

## Current recommended automated LIVE contract

The current recommended automated LIVE runtime is exact:

- runtime mode inside the backend container must resolve to `TRADE_MODE=LIVE`
- authority path must resolve to hub-authoritative mode with `ENABLE_MULTI_HUB=true`, `USE_HUB_ROUTER=true`, and `DISABLE_STREAM_WORKER=false`
- the stream worker provides broker market data, bars, indicators, live marks, and strategy signal generation
- the hub/router/lifecycle/account-runner path remains authoritative for order submission, reconciliation, ownership, lifecycle, and durable control state
- Postgres is authoritative for control-plane state, ownership, lifecycle, outbox, kill-switch durability, sweep state, and EOD state
- LIVE secrets must be sourced from an approved platform secret store; broker credentials may use Postgres. Short-lived injected environment variables or secret file mounts may carry those values into the runtime, but repo env files are not secret sources
- release readiness is judged from the backend container's effective runtime environment and its observed startup/reconciliation behavior, not from the launching shell alone

That distinction matters. A PowerShell session can contain the right values while the backend container still starts with the wrong defaults. Phoenix is ready for automated LIVE only when the container itself resolves the required LIVE tuple and the runtime proves the expected startup guarantees.

---

## Who should operate Phoenix

Phoenix assumes the operator can do all of the following without hidden platform help:

- provision and secure Postgres
- store and rotate SmartAPI credentials
- manage approved LIVE secret sources and the runtime injection process used to launch containers
- maintain tenant, broker account, strategy, and routing rows in the control plane
- collect release evidence before go-live and before major cutovers
- investigate startup failures, reconciliation warnings, stale-mark conditions, and degraded-state transitions

Phoenix is not designed as a beginner-first, wizard-driven deployment product.

---

## Safety model in plain language

Phoenix is built around four practical safety layers.

### 1. Startup must prove the environment is safe

Phoenix should not accept automated live order flow until the backend proves that:

- the runtime really is LIVE
- the authority path is hub-authoritative
- the stream worker is enabled for automated LIVE, or an approved replacement market-data plane exists
- the required durable Postgres stores are reachable
- auth is not disabled
- token secrets are real runtime values, not defaults
- idempotency, ownership, lifecycle persistence, kill-switch durability, daily-loss controls, profit controls, and EOD controls are all enabled

### 2. Order flow is guarded before the broker sees it

Every order passes through capital, risk, kill-switch, exposure, profit, freshness, ownership, broker-session, and circuit-breaker checks before reaching the broker path.

### 3. Runtime recovery prefers evidence over assumptions

Phoenix restores durable internal state first, then reconciles against broker observations. It does not treat one missing poll or one stale snapshot as proof that a live position disappeared.

### 4. Authority boundaries are explicit

The stream worker can generate signals, but it is not the authoritative broker-facing execution plane in hub mode. Only the current authoritative path for a scope may mutate that scope. Browser UI, reports, caches, CSV files, and convenience helpers are not live control authority.

---

## Local and paper usage

The repository still includes local and PAPER-mode assets for development, testing, and controlled validation. Those assets exist to support engineering and dry runs. They are not substitutes for the current automated LIVE path.

The local PAPER examples in this aligned bundle now default to a stream-enabled profile so they better mirror the recommended automated runtime.

---

## Documentation precedence

Read the docs in this order when making a production decision:

1. [`ARCHITECTURE.md`](ARCHITECTURE.md)
2. [Docker Desktop LIVE Deployment](docs/runbooks/docker_desktop_live_deployment.md)
3. [OCI LIVE Deployment](docs/runbooks/oci_live_deployment.md), when operating the OCI Compose path
4. [LIVE Release Evidence](docs/runbooks/release_evidence.md)
5. [Broker credential update runbook](docs/runbooks/update_broker_credentials.md)
6. [Blue/Green cutover](docs/runbooks/blue_green_cutover.md)
7. [Restore drill](docs/runbooks/restore_drill.md)

For emergency operator actions:

8. [Kill switch reference](docs/runbooks/kill_switch.md)
9. [Orphan review resolution](docs/runbooks/resolve_orphan_review.md)
10. [Break-glass flatten](docs/runbooks/break_glass_flatten.md)

The Cloud Run material remains reference and roadmap material until it is explicitly approved in the production contract.
