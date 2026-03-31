# Phoenix v9 Cloud Run Deployment Reference

> **Status:** Reference and roadmap material only. This is not the current bundled go-live path.

This document describes a possible Cloud Run target profile for Phoenix v9. It exists for planning, design review, and future implementation work.

Do **not** treat this document as the current go-live procedure.

The bundled implementation path in this aligned set remains the Docker/Desktop runbook using [`docker-compose.live.single.yml`](../../docker-compose.live.single.yml).

---

## Why this document exists

Cloud Run remains a plausible future deployment target because it can provide:

- managed container execution
- Secret Manager integration
- Cloud SQL connectivity
- simpler image promotion

That said, a Cloud Run path is not production-ready until Phoenix proves all of the following for that path:

1. the backend container resolves the full LIVE tuple inside Cloud Run
2. the selected secret sources are wired end to end and validated at startup
3. the authority path remains hub-authoritative only
4. automated LIVE either runs with `DISABLE_STREAM_WORKER=false` end to end, or an approved replacement market-data/bar/indicator/strategy plane exists
5. broker credential resolution is explicit, deterministic, and fail-closed
6. release evidence exists for the Cloud Run manifest, runtime environment, and recovery behavior
7. `ARCHITECTURE.md` is explicitly revised to approve Cloud Run as a current production path

---

## Current position of this path

At the time of this aligned document set:

- Cloud Run is not the current bundled production path
- Cloud Run remains future-path / roadmap material
- Secret Manager is the natural cloud secret source for this path
- this runbook is design guidance, not an approved release checklist

---

## Target architecture for a future approved Cloud Run path

```text
Internet / Load Balancer
  -> Cloud Run service (backend)
     -> FastAPI + AppRuntime + HubRuntime
     -> live market-data / strategy plane (stream worker or approved replacement)
     -> Cloud SQL Postgres (authoritative operational store)
     -> Google Secret Manager (secret source)
```

### Target characteristics

A future approved Cloud Run path should still preserve the current automated LIVE invariants:

- `TRADE_MODE=LIVE`
- `ENABLE_MULTI_HUB=true`
- `USE_HUB_ROUTER=true`
- `DISABLE_STREAM_WORKER=false` **unless** an approved replacement market-data/bar/indicator/strategy plane exists
- Postgres remains the authoritative operational store
- secrets come from Secret Manager or Postgres
- dashboard auth remains enabled
- demo shortcuts remain disabled

---

## Why the stream-enabled requirement matters in Cloud Run

The current recommended automated LIVE runtime depends on a live mark-data and strategy plane. For Cloud Run approval, Phoenix must prove one of the following:

- the stream worker can hold the required broker session, websocket lifecycle, tick handling, bar construction, and strategy runtime within the Cloud Run deployment model, or
- an explicitly approved replacement plane provides equivalent live marks, bar construction, indicator state, and strategy inputs

Broker position/order polling alone is not enough for the automated LIVE contract.

---

## Reference environment sketch

The example `cloudrun.env` in this bundle is reference material only. It now mirrors the stream-enabled automated LIVE tuple so the target profile matches the architecture more closely.

Typical Cloud Run configuration would still include:

- Cloud SQL / Postgres connection settings
- Secret Manager bindings for admin, DB, auth-token, and any broker secrets not stored in Postgres
- explicit LIVE feature flags
- revision-based rollout and rollback control

---

## Release evidence that would be required before approval

A future Cloud Run approval would need, at minimum:

- rendered Cloud Run service specification
- effective runtime env inside the running container
- startup validation success evidence
- evidence of stream-worker or replacement market-data plane health
- reconciliation / recovery evidence across restart
- cutover and rollback playbook evidence
- security review showing secret handling matches the architecture rules

Until that evidence exists, keep using the bundled Docker/Desktop implementation runbook for practical deployment guidance.
