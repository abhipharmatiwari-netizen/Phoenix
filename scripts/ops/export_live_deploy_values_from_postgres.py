from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Mapping

try:  # pragma: no cover - import availability is environment-dependent
    import psycopg  # type: ignore
    from psycopg.conninfo import make_conninfo  # type: ignore
    from psycopg.rows import dict_row  # type: ignore
except Exception as exc:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


_CAPITAL_LIMIT_KEYS = {"max_notional_per_order", "max_gross_exposure"}


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _capital_limits_from_meta(
    meta: Mapping[str, Any],
    *,
    tenant_id: str,
    broker_account_id: str,
) -> str | None:
    raw = meta.get("capital_limits_json")
    if raw:
        data = _as_dict(raw)
        if data:
            return json.dumps(data, separators=(",", ":"))

    raw_limits = meta.get("capital_limits")
    limits = _as_dict(raw_limits)
    if not limits:
        return None

    if any(key in limits for key in _CAPITAL_LIMIT_KEYS):
        data = {f"{tenant_id}:{broker_account_id}": limits}
    else:
        data = limits
    return json.dumps(data, separators=(",", ":"))


def _risk_max_daily_loss_from_meta(meta: Mapping[str, Any]) -> str | None:
    for key in ("risk_max_daily_loss", "max_daily_loss"):
        value = meta.get(key)
        if value not in (None, ""):
            return str(value)
    risk = _as_dict(meta.get("risk"))
    for key in ("risk_max_daily_loss", "max_daily_loss"):
        value = risk.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _conninfo_from_env() -> str:
    password = (
        os.getenv("CONTROL_PLANE_PG_PASSWORD_HOST")
        or os.getenv("CONTROL_PLANE_PG_PASSWORD")
        or ""
    )
    missing = [
        name
        for name, value in {
            "CONTROL_PLANE_PG_HOST": os.getenv("CONTROL_PLANE_PG_HOST"),
            "CONTROL_PLANE_PG_DB": os.getenv("CONTROL_PLANE_PG_DB"),
            "CONTROL_PLANE_PG_USER": os.getenv("CONTROL_PLANE_PG_USER"),
            "CONTROL_PLANE_PG_PASSWORD_HOST": password,
        }.items()
        if not str(value or "").strip()
    ]
    if missing:
        raise RuntimeError(
            "Missing Postgres bootstrap settings: " + ", ".join(missing)
        )
    return make_conninfo(
        host=os.getenv("CONTROL_PLANE_PG_HOST"),
        port=int(os.getenv("CONTROL_PLANE_PG_PORT") or "5432"),
        dbname=os.getenv("CONTROL_PLANE_PG_DB"),
        user=os.getenv("CONTROL_PLANE_PG_USER"),
        password=password,
        sslmode=os.getenv("CONTROL_PLANE_PG_SSLMODE") or None,
        connect_timeout=5,
    )


def fetch_deploy_values(
    *,
    tenant_id: str,
    broker_account_id: str,
) -> dict[str, Any]:
    if psycopg is None or dict_row is None:
        raise RuntimeError(f"psycopg is required: {_IMPORT_ERROR}")

    sql = """
        SELECT
            ba.tenant_id,
            ba.meta,
            bc.client_local_ip,
            bc.client_public_ip,
            bc.mac_address
        FROM broker_accounts ba
        LEFT JOIN broker_credentials bc
          ON bc.broker_account_id = ba.broker_account_id
        WHERE ba.broker_account_id = %s
          AND ba.tenant_id = %s
        LIMIT 1
    """
    with psycopg.connect(_conninfo_from_env(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (broker_account_id, tenant_id))
            row = cur.fetchone()

    if row is None:
        raise RuntimeError(
            "No broker_accounts row found for "
            f"tenant_id={tenant_id} broker_account_id={broker_account_id}"
        )

    meta = _as_dict(row.get("meta"))
    return {
        "tenant_id": tenant_id,
        "broker_account_id": broker_account_id,
        "capital_limits_json": _capital_limits_from_meta(
            meta,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
        ),
        "risk_max_daily_loss": _risk_max_daily_loss_from_meta(meta),
        "client_local_ip": row.get("client_local_ip") or "",
        "client_public_ip": row.get("client_public_ip") or "",
        "mac_address": row.get("mac_address") or "",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export non-printed Docker LIVE deploy values from Postgres."
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--broker-account-id", required=True)
    args = parser.parse_args(argv)
    try:
        values = fetch_deploy_values(
            tenant_id=args.tenant_id,
            broker_account_id=args.broker_account_id,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(values, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
