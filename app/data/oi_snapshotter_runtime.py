"""Opt-in runtime wiring for the OI option-chain snapshotter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import os
from typing import Mapping

from app.core import angel_login
from app.core.instruments_resolver import load_scrip_master
from app.core.order_client import AngelOrderClient
from app.data.angel_option_chain_provider import AngelOptionChainProvider
from app.data.oi_snapshotter import OiSnapshotter, SnapshotResult
from app.data.option_chain_store import OptionChainStore
from app.data.postgres import connect_with_retry, get_control_plane_dsn


@dataclass(frozen=True)
class OiSnapshotterRuntimeConfig:
    enabled: bool
    provider: str
    underlying: str
    expiry: date
    dsn: str | None = None
    once: bool = False
    start_time: time = time(9, 15)
    end_time: time = time(15, 30)
    cadence_seconds: int = 60
    max_snapshots: int | None = None
    batch_size: int = 50


def load_runtime_config(
    *,
    env: Mapping[str, str] | None = None,
    enabled: bool | None = None,
    provider: str | None = None,
    underlying: str | None = None,
    expiry: str | date | None = None,
    dsn: str | None = None,
    once: bool | None = None,
    max_snapshots: int | None = None,
) -> OiSnapshotterRuntimeConfig:
    source = env or os.environ
    resolved_enabled = _bool_value(
        enabled if enabled is not None else source.get("OI_SNAPSHOTTER_ENABLED"),
        default=False,
    )
    resolved_provider = str(provider or source.get("OI_SNAPSHOTTER_PROVIDER") or "angel").strip().lower()
    resolved_underlying = str(
        underlying or source.get("OI_SNAPSHOTTER_UNDERLYING") or "NIFTY"
    ).strip().upper()
    resolved_expiry = _parse_date(expiry or source.get("OI_SNAPSHOTTER_EXPIRY"))
    if resolved_expiry is None:
        raise ValueError("OI snapshotter requires --expiry or OI_SNAPSHOTTER_EXPIRY")
    return OiSnapshotterRuntimeConfig(
        enabled=resolved_enabled,
        provider=resolved_provider,
        underlying=resolved_underlying,
        expiry=resolved_expiry,
        dsn=dsn or source.get("OI_SNAPSHOTTER_PG_DSN") or source.get("OPTION_CHAIN_PG_DSN") or None,
        once=_bool_value(once if once is not None else source.get("OI_SNAPSHOTTER_ONCE"), default=False),
        start_time=_parse_time(source.get("OI_SNAPSHOTTER_START_TIME"), time(9, 15)),
        end_time=_parse_time(source.get("OI_SNAPSHOTTER_END_TIME"), time(15, 30)),
        cadence_seconds=_int_value(source.get("OI_SNAPSHOTTER_CADENCE_SECONDS"), 60, minimum=1),
        max_snapshots=(
            max_snapshots
            if max_snapshots is not None
            else _optional_int(source.get("OI_SNAPSHOTTER_MAX_SNAPSHOTS"), minimum=1)
        ),
        batch_size=_int_value(source.get("OI_SNAPSHOTTER_BATCH_SIZE"), 50, minimum=1),
    )


def run_runtime(config: OiSnapshotterRuntimeConfig) -> list[SnapshotResult]:
    if not config.enabled:
        return []
    if config.provider != "angel":
        raise ValueError(f"unsupported OI snapshotter provider={config.provider!r}")

    tokens = angel_login.angel_login_and_get_tokens()
    client = AngelOrderClient(
        jwt_token=tokens["jwtToken"],
        api_key=tokens["API_KEY"],
        client_local_ip=tokens.get("client_local_ip"),
        client_public_ip=tokens.get("client_public_ip"),
        mac_address=tokens.get("mac_address"),
    )
    provider = AngelOptionChainProvider(
        quote_fetcher=client,
        scrip_master=load_scrip_master(),
        batch_size=config.batch_size,
    )
    dsn = config.dsn or get_control_plane_dsn()
    with connect_with_retry(dsn, autocommit=False) as conn:
        snapshotter = OiSnapshotter(
            provider=provider,
            store=OptionChainStore(conn, commit=True),
        )
        if config.once:
            return [
                snapshotter.capture_once(
                    underlying=config.underlying,
                    expiry=config.expiry,
                )
            ]
        return snapshotter.run_session(
            underlying=config.underlying,
            expiry=config.expiry,
            start_time=config.start_time,
            end_time=config.end_time,
            cadence_seconds=config.cadence_seconds,
            max_snapshots=config.max_snapshots,
        )


def _parse_date(value: str | date | None) -> date | None:
    if isinstance(value, date):
        return value
    if value in (None, ""):
        return None
    return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()


def _parse_time(value: str | None, default: time) -> time:
    if value in (None, ""):
        return default
    hour, minute = (int(part) for part in str(value).split(":", maxsplit=1))
    return time(hour, minute)


def _bool_value(value: object, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int_value(value: object, default: int, *, minimum: int) -> int:
    try:
        parsed = int(value) if value not in (None, "") else int(default)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), parsed)


def _optional_int(value: object, *, minimum: int) -> int | None:
    if value in (None, ""):
        return None
    return _int_value(value, int(minimum), minimum=minimum)


__all__ = [
    "OiSnapshotterRuntimeConfig",
    "load_runtime_config",
    "run_runtime",
]
