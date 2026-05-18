"""Opt-in runtime wiring for the OI option-chain snapshotter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import logging
import os
import threading
from typing import Mapping

from app.core import angel_login
from app.core.instruments_resolver import load_scrip_master
from app.core.order_client import AngelOrderClient
from app.data.angel_option_chain_provider import AngelOptionChainProvider
from app.data.oi_snapshotter import OiSnapshotter, SnapshotResult
from app.data.nse_option_chain_provider import NseOptionChainProvider, NseWebOptionChainClient
from app.data.option_chain_realtime_validator import (
    RealtimeOptionChainValidationConfig,
    RealtimeOptionChainValidator,
)
from app.data.option_chain_store import OptionChainStore
from app.data.option_chain_validation import OptionChainValidationConfig
from app.data.option_chain_validation_store import OptionChainValidationReportStore
from app.data.postgres import connect_with_retry, get_control_plane_dsn

logger = logging.getLogger(__name__)


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
    reuse_broker_session: bool = True
    session_max_age_minutes: int = 600
    nse_validation_enabled: bool = False
    nse_validation_store_quotes: bool = True
    nse_validation_log_all: bool = True
    nse_validation_fail_on_error: bool = False
    nse_validation_timeout_seconds: int = 10
    nse_validation_oi_abs_tolerance: int = 0
    nse_validation_volume_abs_tolerance: int = 250
    nse_validation_volume_pct_tolerance: float = 0.05
    nse_validation_price_abs_tolerance: float = 0.10
    nse_validation_price_pct_tolerance: float = 0.01
    nse_validation_iv_abs_tolerance: float = 0.50
    nse_validation_iv_pct_tolerance: float = 0.05


@dataclass(frozen=True)
class _CachedAngelQuoteClient:
    client: AngelOrderClient
    created_at_utc: datetime
    broker_account_id: str | None


_CLIENT_CACHE_LOCK = threading.Lock()
_CACHED_CLIENT: _CachedAngelQuoteClient | None = None


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
        reuse_broker_session=_bool_value(
            source.get("OI_SNAPSHOTTER_REUSE_BROKER_SESSION"),
            default=True,
        ),
        session_max_age_minutes=_int_value(
            source.get("OI_SNAPSHOTTER_SESSION_MAX_AGE_MINUTES"),
            600,
            minimum=5,
        ),
        nse_validation_enabled=_bool_value(
            source.get("OI_ML_ENABLE_NSE_VALIDATION")
            or source.get("OI_CHAIN_NSE_VALIDATION_ENABLED"),
            default=False,
        ),
        nse_validation_store_quotes=_bool_value(
            source.get("OI_ML_NSE_VALIDATION_STORE_QUOTES"),
            default=True,
        ),
        nse_validation_log_all=_bool_value(
            source.get("OI_ML_NSE_VALIDATION_LOG_ALL"),
            default=True,
        ),
        nse_validation_fail_on_error=_bool_value(
            source.get("OI_ML_NSE_VALIDATION_FAIL_ON_ERROR"),
            default=False,
        ),
        nse_validation_timeout_seconds=_int_value(
            source.get("OI_ML_NSE_VALIDATION_TIMEOUT_SECONDS"),
            10,
            minimum=1,
        ),
        nse_validation_oi_abs_tolerance=_int_value(
            source.get("OI_ML_NSE_VALIDATION_OI_ABS_TOLERANCE"),
            0,
            minimum=0,
        ),
        nse_validation_volume_abs_tolerance=_int_value(
            source.get("OI_ML_NSE_VALIDATION_VOLUME_ABS_TOLERANCE"),
            250,
            minimum=0,
        ),
        nse_validation_volume_pct_tolerance=_float_value(
            source.get("OI_ML_NSE_VALIDATION_VOLUME_PCT_TOLERANCE"),
            0.05,
            minimum=0.0,
        ),
        nse_validation_price_abs_tolerance=_float_value(
            source.get("OI_ML_NSE_VALIDATION_PRICE_ABS_TOLERANCE"),
            0.10,
            minimum=0.0,
        ),
        nse_validation_price_pct_tolerance=_float_value(
            source.get("OI_ML_NSE_VALIDATION_PRICE_PCT_TOLERANCE"),
            0.01,
            minimum=0.0,
        ),
        nse_validation_iv_abs_tolerance=_float_value(
            source.get("OI_ML_NSE_VALIDATION_IV_ABS_TOLERANCE"),
            0.50,
            minimum=0.0,
        ),
        nse_validation_iv_pct_tolerance=_float_value(
            source.get("OI_ML_NSE_VALIDATION_IV_PCT_TOLERANCE"),
            0.05,
            minimum=0.0,
        ),
    )


def run_runtime(config: OiSnapshotterRuntimeConfig) -> list[SnapshotResult]:
    if not config.enabled:
        return []
    if config.provider != "angel":
        raise ValueError(f"unsupported OI snapshotter provider={config.provider!r}")

    client = _get_angel_quote_client(config)
    provider = AngelOptionChainProvider(
        quote_fetcher=client,
        scrip_master=load_scrip_master(),
        batch_size=config.batch_size,
    )
    dsn = config.dsn or get_control_plane_dsn()
    with connect_with_retry(dsn, autocommit=False) as conn:
        store = OptionChainStore(conn, commit=True)
        validator = _build_nse_validator(config, conn) if config.nse_validation_enabled else None
        if validator is None:
            snapshotter = OiSnapshotter(provider=provider, store=store)
        else:
            snapshotter = OiSnapshotter(provider=provider, store=store, validator=validator)
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


def _build_nse_validator(
    config: OiSnapshotterRuntimeConfig,
    conn: object,
) -> RealtimeOptionChainValidator:
    nse_provider = NseOptionChainProvider(
        NseWebOptionChainClient(
            timeout_seconds=float(config.nse_validation_timeout_seconds),
        )
    )
    quote_store = OptionChainStore(conn, commit=True) if config.nse_validation_store_quotes else None
    return RealtimeOptionChainValidator(
        reference_provider=nse_provider,
        reference_quote_store=quote_store,
        report_store=OptionChainValidationReportStore(conn, commit=True),
        config=RealtimeOptionChainValidationConfig(
            enabled=True,
            store_reference_quotes=config.nse_validation_store_quotes,
            log_all_observations=config.nse_validation_log_all,
            fail_on_error=config.nse_validation_fail_on_error,
            validation_config=OptionChainValidationConfig(
                oi_abs_tolerance=config.nse_validation_oi_abs_tolerance,
                volume_abs_tolerance=config.nse_validation_volume_abs_tolerance,
                volume_pct_tolerance=config.nse_validation_volume_pct_tolerance,
                price_abs_tolerance=config.nse_validation_price_abs_tolerance,
                price_pct_tolerance=config.nse_validation_price_pct_tolerance,
                iv_abs_tolerance=config.nse_validation_iv_abs_tolerance,
                iv_pct_tolerance=config.nse_validation_iv_pct_tolerance,
            ),
        ),
    )


def _get_angel_quote_client(config: OiSnapshotterRuntimeConfig) -> AngelOrderClient:
    if not config.reuse_broker_session:
        return _login_angel_quote_client()

    global _CACHED_CLIENT
    now_utc = datetime.now(timezone.utc)
    with _CLIENT_CACHE_LOCK:
        if _CACHED_CLIENT is not None and not _cache_expired(
            _CACHED_CLIENT,
            now_utc=now_utc,
            max_age_minutes=config.session_max_age_minutes,
        ):
            return _CACHED_CLIENT.client

        client = _login_angel_quote_client()
        _CACHED_CLIENT = _CachedAngelQuoteClient(
            client=client,
            created_at_utc=now_utc,
            broker_account_id=os.getenv("HUB_DEFAULT_BROKER_ACCOUNT_ID") or None,
        )
        return client


def _login_angel_quote_client() -> AngelOrderClient:
    tokens = angel_login.angel_login_and_get_tokens()
    broker_account_id = os.getenv("HUB_DEFAULT_BROKER_ACCOUNT_ID") or None
    proxy_configured = bool(
        os.getenv("ANGEL_HTTPS_PROXY")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
    )
    logger.info(
        "oi_snapshotter Angel quote session established broker_account_id=%s proxy_configured=%s",
        broker_account_id or "default",
        proxy_configured,
    )
    return AngelOrderClient(
        jwt_token=tokens["jwtToken"],
        api_key=tokens["API_KEY"],
        broker_account_id=broker_account_id,
        client_local_ip=tokens.get("client_local_ip"),
        client_public_ip=tokens.get("client_public_ip"),
        mac_address=tokens.get("mac_address"),
    )


def _cache_expired(
    cached: _CachedAngelQuoteClient,
    *,
    now_utc: datetime,
    max_age_minutes: int,
) -> bool:
    created = cached.created_at_utc
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    max_age = timedelta(minutes=max(5, int(max_age_minutes)))
    return now_utc - created >= max_age


def clear_cached_angel_quote_client() -> None:
    """Clear the cached Angel quote session.

    Primarily useful for tests and supervised recovery tooling. The sidecar
    still creates no order-capable route; this cache only avoids repeated
    read-only quote logins during the same market session.
    """
    global _CACHED_CLIENT
    with _CLIENT_CACHE_LOCK:
        _CACHED_CLIENT = None


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


def _float_value(value: object, default: float, *, minimum: float) -> float:
    try:
        parsed = float(value) if value not in (None, "") else float(default)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(float(minimum), parsed)


def _optional_int(value: object, *, minimum: int) -> int | None:
    if value in (None, ""):
        return None
    return _int_value(value, int(minimum), minimum=minimum)


__all__ = [
    "OiSnapshotterRuntimeConfig",
    "clear_cached_angel_quote_client",
    "load_runtime_config",
    "run_runtime",
]
