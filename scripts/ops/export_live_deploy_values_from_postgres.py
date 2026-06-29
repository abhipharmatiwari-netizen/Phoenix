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
_REQUIRED_BROKER_CREDENTIAL_FIELDS = (
    "api_key",
    "client_code",
    "pin",
    "totp_secret",
)
_REQUIRED_BROKER_NETWORK_FIELDS = (
    "client_local_ip",
    "client_public_ip",
    "mac_address",
)


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


def _capital_limits_json_has_account_key(
    capital_limits_json: str,
    *,
    tenant_id: str,
    broker_account_id: str,
) -> bool:
    data = _as_dict(capital_limits_json)
    return (
        f"{tenant_id}:{broker_account_id}" in data
        or str(broker_account_id) in data
    )


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


def _missing_required_fields(
    row: Mapping[str, Any],
    fields: tuple[str, ...],
) -> list[str]:
    return [
        field
        for field in fields
        if not str(row.get(field) or "").strip()
    ]


def _conninfo_from_env() -> str:
    host = os.getenv("CONTROL_PLANE_PG_HOST")
    if str(host or "").strip().lower() == "host.docker.internal":
        host = os.getenv("CONTROL_PLANE_PG_HOST_LOCAL") or "127.0.0.1"
    password = (
        os.getenv("CONTROL_PLANE_PG_PASSWORD_HOST")
        or os.getenv("CONTROL_PLANE_PG_PASSWORD")
        or ""
    )
    missing = [
        name
        for name, value in {
            "CONTROL_PLANE_PG_HOST": host,
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
        host=host,
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
            bc.api_key,
            bc.client_code,
            bc.pin,
            bc.totp_secret,
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
    missing_credential_fields = _missing_required_fields(
        row,
        _REQUIRED_BROKER_CREDENTIAL_FIELDS,
    )
    if missing_credential_fields:
        raise RuntimeError(
            "broker_credentials missing required Postgres broker secret fields "
            f"for tenant_id={tenant_id} broker_account_id={broker_account_id}: "
            + ", ".join(missing_credential_fields)
        )

    missing_network_fields = _missing_required_fields(
        row,
        _REQUIRED_BROKER_NETWORK_FIELDS,
    )
    if missing_network_fields:
        raise RuntimeError(
            "broker_credentials missing required broker network identity fields "
            f"for tenant_id={tenant_id} broker_account_id={broker_account_id}: "
            + ", ".join(missing_network_fields)
        )

    capital_limits_json = _capital_limits_from_meta(
        meta,
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
    )
    if not capital_limits_json:
        raise RuntimeError(
            "broker_accounts.meta missing account-specific capital_limits "
            f"for tenant_id={tenant_id} broker_account_id={broker_account_id}"
        )
    if not _capital_limits_json_has_account_key(
        capital_limits_json,
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
    ):
        raise RuntimeError(
            "broker_accounts.meta capital_limits_json must include an account key "
            f"for {tenant_id}:{broker_account_id}"
        )

    return {
        "tenant_id": tenant_id,
        "broker_account_id": broker_account_id,
        "broker_credentials_ready": True,
        "capital_limits_json": capital_limits_json,
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
