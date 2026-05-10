"""
AngelOne BrokerClient implementation.

Wraps the existing angel_login and order_client modules so that the rest
of the hub/strategies can talk to a clean BrokerClient interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, List, Optional

from app.brokers.base import (
    Balance,
    BrokerClient,
    OrderRequest,
    OrderResponse,
    OrderStatus,
    Position,
    ProductType,
)
from app.brokers.errors import BrokerSchemaError
from app.brokers.http_utils import is_html_block
from app.brokers.positions_types import PositionsFetchResult, PositionsStatus
from app.brokers.secrets import AngelSecrets
from app.core import angel_login
from app.core import order_client as core_order_client
from app.core.feature_flags import load_stability_feature_flags
from app.core.identifiers import BrokerAccountId, TenantId
from app.core.logging_utils import log_event
from app.tenants.models import BrokerAccountModel

logger = logging.getLogger(__name__)

_TERMINAL_ORDER_STATUSES = {
    "COMPLETE",
    "COMPLETED",
    "REJECTED",
    "CANCELLED",
    "CANCELED",
}

_PENDING_TIMEOUT_BROKER_ORDER_ID = "PENDING_TIMEOUT_RECONCILIATION"


# Cross-client (per-tenant) fetch cooldowns (single Cloud Run instance may host multiple clients).
_TENANT_FETCH_COOLDOWN_UNTIL: dict[tuple[str, str], float] = {}
_TENANT_FETCH_COOLDOWN_LOCK = threading.Lock()


def _tenant_get_cooldown_until(tenant_id: str, channel: str) -> float:
    key = (tenant_id, channel)
    with _TENANT_FETCH_COOLDOWN_LOCK:
        return _TENANT_FETCH_COOLDOWN_UNTIL.get(key, 0.0)


def _tenant_set_cooldown_until(tenant_id: str, channel: str, until_ts: float) -> None:
    # until_ts is a time.monotonic() timestamp.
    key = (tenant_id, channel)
    now = time.monotonic()
    with _TENANT_FETCH_COOLDOWN_LOCK:
        if until_ts <= now:
            _TENANT_FETCH_COOLDOWN_UNTIL.pop(key, None)
            return
        current = _TENANT_FETCH_COOLDOWN_UNTIL.get(key, 0.0)
        if until_ts > current:
            _TENANT_FETCH_COOLDOWN_UNTIL[key] = until_ts



class AngelBrokerError(Exception):
    pass


class AngelBlockedResponseError(AngelBrokerError):
    pass


class AngelPositionsFetchError(AngelBrokerError):
    pass


def _safe_snippet(value: Any, limit: int = 512) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=True)
        except Exception:
            text = str(value)
    elif isinstance(value, bytes):
        text = value.decode("utf-8", errors="ignore")
    else:
        text = str(value)
    compact = " ".join(text.strip().split())
    return compact[:limit]


def _is_rate_limited_message(text: str) -> bool:
    return "exceeding access rate" in text.lower()


# Module-level counter for balance schema violations across all broker clients.
# Readyz can check this to detect persistent balance schema failures.
_global_balance_schema_violation_count: int = 0


def get_balance_schema_violation_count() -> int:
    """Return the total number of CRITICAL balance schema violations since process start."""
    return _global_balance_schema_violation_count


def _is_blocked_exception(exc: Exception) -> bool:
    if isinstance(exc, AngelBlockedResponseError):
        return True
    if isinstance(exc, core_order_client.AngelHTTPBlockedError):
        return True
    return is_html_block(str(exc), None)


class FetchFailureKind(str, Enum):
    AUTH = "auth"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    BLOCKED = "blocked"
    OTHER = "other"


@dataclass(frozen=True)
class FetchFailure:
    kind: FetchFailureKind
    message: str
    http_status: Optional[int] = None
    retry_after_seconds: Optional[float] = None


_AUTH_MARKERS = (
    "invalid token",
    "token expired",
    "jwt expired",
    "unauthorized",
    "not authorized",
    "session expired",
    "invalid session",
    "authentication failed",
    "invalid jwt",
)


def _looks_like_auth_error(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _AUTH_MARKERS)


_BROKER_POLICY_BLOCK_MARKERS = (
    "unregistered ip address",
    "register your ip before retrying",
    "ip address is not allowed",
    "ip not whitelisted",
)


def _looks_like_broker_policy_block(text: str) -> bool:
    low = (text or "").lower()
    if "ag7002" in low:
        return True
    return any(marker in low for marker in _BROKER_POLICY_BLOCK_MARKERS)


def _is_timeout_exception(exc: Exception) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, socket.timeout)):
        return True
    msg = str(exc).lower()
    return ("timed out" in msg) or ("readtimeout" in msg) or ("timeout" in msg and "connect" in msg)


def _classify_fetch_exception(exc: Exception) -> FetchFailure:
    http_status = getattr(exc, "status", None)
    retry_after = getattr(exc, "retry_after_seconds", None)
    msg = str(exc)
    low = msg.lower()

    # AngelHTTPBlockedError includes rate limits + HTML/Non-JSON/empty bodies.
    if isinstance(exc, core_order_client.AngelHTTPBlockedError):
        if (http_status in (403, 429) and _is_rate_limited_message(low)) or "rate limit" in low:
            return FetchFailure(
                kind=FetchFailureKind.RATE_LIMIT,
                message=msg,
                http_status=http_status,
                retry_after_seconds=retry_after,
            )
        return FetchFailure(
            kind=FetchFailureKind.BLOCKED,
            message=msg,
            http_status=http_status,
            retry_after_seconds=retry_after,
        )

    if _is_timeout_exception(exc):
        return FetchFailure(
            kind=FetchFailureKind.TIMEOUT,
            message=msg,
            http_status=http_status,
            retry_after_seconds=retry_after,
        )

    if http_status in (401, 403) and not _is_rate_limited_message(low):
        return FetchFailure(
            kind=FetchFailureKind.AUTH,
            message=msg,
            http_status=http_status,
            retry_after_seconds=retry_after,
        )

    if _looks_like_auth_error(low):
        return FetchFailure(
            kind=FetchFailureKind.AUTH,
            message=msg,
            http_status=http_status,
            retry_after_seconds=retry_after,
        )

    if _looks_like_broker_policy_block(low):
        return FetchFailure(
            kind=FetchFailureKind.BLOCKED,
            message=msg,
            http_status=http_status,
            retry_after_seconds=retry_after,
        )

    if _is_blocked_exception(exc):
        return FetchFailure(
            kind=FetchFailureKind.BLOCKED,
            message=msg,
            http_status=http_status,
            retry_after_seconds=retry_after,
        )

    return FetchFailure(
        kind=FetchFailureKind.OTHER,
        message=msg,
        http_status=http_status,
        retry_after_seconds=retry_after,
    )


class AngelBrokerClient(BrokerClient):
    def __init__(self, account: BrokerAccountModel, secrets: AngelSecrets) -> None:
        self._account = account
        self._secrets = secrets
        self._order_client: core_order_client.AngelOrderClient | None = None
        self._broker_account_id: BrokerAccountId = account.broker_account_id
        self._tenant_id: TenantId = account.tenant_id
        self._login_lock = asyncio.Lock()
        self._logged_in_at: Optional[datetime] = None  # UTC timestamp of last successful login

        # Fetch storm protection (per-account). These guards prevent
        # "timeout -> relogin -> timeout -> ..." loops and reduce WAF/rate-limit pressure.
        self._clear_cooldown(channel="balance")
        self._clear_cooldown(channel="orders")
        self._clear_cooldown(channel="positions")

        self._balance_fail_count = 0
        self._orders_fail_count = 0
        self._positions_fail_count = 0
        self._balance_schema_violation_count = 0

        self._last_balance: Optional[Balance] = None
        self._last_orders: Optional[List[OrderStatus]] = None
        self._last_positions_result: Optional[PositionsFetchResult] = None

        # Order caching during cooldown (Fix #3)
        self._order_cache: Optional[List[OrderStatus]] = None
        self._order_cache_time: float = 0.0
        self._order_cache_ttl: float = 10.0  # Cache orders for 10 seconds during cooldown

        # Existing counters
        self._positions_blocked_count = 0
        self._orders_blocked_until = 0.0
        self._orders_blocked_count = 0
        try:
            self._order_place_max_attempts = max(
                1, int(os.getenv("ANGEL_ORDER_PLACE_MAX_ATTEMPTS", "2"))
            )
        except Exception:
            self._order_place_max_attempts = 2
        try:
            self._order_place_retry_base_seconds = max(
                0.1, float(os.getenv("ANGEL_ORDER_PLACE_RETRY_BASE_SECONDS", "0.5"))
            )
        except Exception:
            self._order_place_retry_base_seconds = 0.5

    async def _call(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def login(self) -> None:
        """Login once per process unless forced.

        This prevents repeated TOTP logins (and downstream control-plane churn)
        while still allowing a refresh when we detect auth/session errors.
        """
        await self._login(force=False)

    async def _login(self, *, force: bool) -> None:
        async with self._login_lock:
            if self._order_client is not None and not force:
                return
            try:
                tokens = angel_login.angel_login_with_secrets(self._secrets)
            except Exception:
                log_event(
                    logger,
                    event_type="LOGIN_FAILED",
                    message="Angel login failed.",
                    tenant_id=self._tenant_id,
                    broker_account_id=self._broker_account_id,
                    level=logging.ERROR,
                )
                raise
            self._order_client = core_order_client.AngelOrderClient(
                jwt_token=tokens.jwt_token,
                api_key=self._secrets.api_key,
                broker_account_id=str(self._broker_account_id),
                client_local_ip=self._secrets.client_local_ip,
                client_public_ip=self._secrets.client_public_ip,
                mac_address=self._secrets.mac_address,
            )
            self._logged_in_at = datetime.now(timezone.utc)
            log_event(
                logger,
                event_type="LOGIN_SUCCESS",
                message="Angel login succeeded.",
                tenant_id=self._tenant_id,
                broker_account_id=self._broker_account_id,
                level=logging.INFO,
            )
            # §103: Do not log client_code — it is a broker account identifier
            # that should not appear in log files or aggregated log streams.
            logger.info(
                "AngelBrokerClient login successful for broker_account_id=%s",
                self._account.broker_account_id,
            )

    def is_token_near_expiry(
        self,
        *,
        margin_minutes: int = 10,
        expiry_ist_hour: int = 0,
        expiry_ist_minute: int = 0,
        tz_offset_hours: int = 5,
        tz_offset_minutes: int = 30,
    ) -> bool:
        """Return True if the Angel auth token is already expired or will expire within *margin_minutes*.

        Tokens expire daily at 00:00 IST (18:30 UTC previous day).
        Returns True in two cases:
        1. The token was issued before the last 00:00 IST boundary — it is already stale.
        2. The next 00:00 IST boundary is within *margin_minutes* — proactive refresh before the window.
        """
        if self._logged_in_at is None:
            return False
        now_utc = datetime.now(timezone.utc)
        ist_offset = timedelta(hours=tz_offset_hours, minutes=tz_offset_minutes)
        now_ist = now_utc + ist_offset
        today_ist = now_ist.date()

        # Midnight IST for the current IST calendar day expressed in UTC.
        today_midnight_utc = datetime(
            today_ist.year, today_ist.month, today_ist.day,
            expiry_ist_hour, expiry_ist_minute, 0,
            tzinfo=timezone.utc,
        ) - ist_offset

        # The most recent past midnight IST boundary.
        if today_midnight_utc <= now_utc:
            last_midnight_utc = today_midnight_utc
        else:
            last_midnight_utc = today_midnight_utc - timedelta(days=1)

        # Case 1: token was issued before the last midnight IST — already expired.
        if self._logged_in_at < last_midnight_utc:
            return True

        # Case 2: next midnight IST is approaching within the margin.
        next_midnight_utc = last_midnight_utc + timedelta(days=1)
        seconds_to_expiry = (next_midnight_utc - now_utc).total_seconds()
        return 0 < seconds_to_expiry <= margin_minutes * 60

    async def proactive_relogin_if_near_expiry(self, *, margin_minutes: int = 10) -> bool:
        """Re-login proactively if the token is within *margin_minutes* of the daily 00:00 IST reset boundary.

        Returns True if a re-login was performed, False otherwise.
        This prevents the 8-hour window of AG8001 Invalid Token errors caused by
        reacting only after the token has already expired at midnight IST (§79).
        """
        if not self.is_token_near_expiry(margin_minutes=margin_minutes):
            return False
        logger.info(
            "broker.proactive_relogin: token for broker_account_id=%s is within %dm of "
            "midnight IST expiry — refreshing before the auth error window",
            self._broker_account_id,
            margin_minutes,
        )
        try:
            await self._login(force=True)
            logger.info(
                "broker.proactive_relogin_success: broker_account_id=%s token refreshed",
                self._broker_account_id,
            )
            return True
        except Exception as exc:
            logger.error(
                "broker.proactive_relogin_failed: broker_account_id=%s error=%s — "
                "will rely on reactive relogin when auth errors occur",
                self._broker_account_id,
                exc,
            )
            return False



    def _cooldown_remaining(self, until_ts: float, *, channel: str) -> float:
        tenant_until = _tenant_get_cooldown_until(str(self._tenant_id), channel)
        until = max(until_ts, tenant_until)
        now = time.monotonic()
        return max(0.0, until - now)

    def _set_cooldown(self, *, channel: str, seconds: float) -> None:
        seconds = max(0.0, float(seconds))
        until = time.monotonic() + seconds
        if channel == "balance":
            self._balance_fetch_cooldown_until = max(self._balance_fetch_cooldown_until, until)
            _tenant_set_cooldown_until(str(self._tenant_id), channel, self._balance_fetch_cooldown_until)
            return
        if channel == "orders":
            self._orders_fetch_cooldown_until = max(self._orders_fetch_cooldown_until, until)
            _tenant_set_cooldown_until(str(self._tenant_id), channel, self._orders_fetch_cooldown_until)
            return
        if channel == "positions":
            self._positions_fetch_cooldown_until = max(self._positions_fetch_cooldown_until, until)
            _tenant_set_cooldown_until(str(self._tenant_id), channel, self._positions_fetch_cooldown_until)
            return

    def _clear_cooldown(self, *, channel: str) -> None:
        """Clear (reset) the cooldown timer for a fetch channel.

        IMPORTANT: This must never call itself; doing so causes infinite recursion
        and crashes Cloud Run during FastAPI startup.
        """
        local_clear_only = self._tenant_cooldown_local_clear_only_enabled()
        tenant_remaining = max(
            0.0,
            _tenant_get_cooldown_until(str(self._tenant_id), channel) - time.monotonic(),
        )

        def _log_local_clear() -> None:
            if not local_clear_only or tenant_remaining <= 0.0:
                return
            log_event(
                logger,
                event_type="FETCH_COOLDOWN_LOCAL_CLEAR_ONLY",
                message="Cleared local cooldown while preserving tenant-wide cooldown.",
                tenant_id=self._tenant_id,
                broker_account_id=self._broker_account_id,
                level=logging.INFO,
                channel=channel,
                tenant_cooldown_remaining_seconds=round(tenant_remaining, 3),
            )

        if channel == "balance":
            self._balance_fetch_cooldown_until = 0.0
            if not local_clear_only:
                _tenant_set_cooldown_until(str(self._tenant_id), channel, 0.0)
            _log_local_clear()
            return

        if channel == "orders":
            self._orders_fetch_cooldown_until = 0.0
            if not local_clear_only:
                _tenant_set_cooldown_until(str(self._tenant_id), channel, 0.0)
            _log_local_clear()
            return

        if channel == "positions":
            self._positions_fetch_cooldown_until = 0.0
            if not local_clear_only:
                _tenant_set_cooldown_until(str(self._tenant_id), channel, 0.0)
            _log_local_clear()
            return

        # Unknown channel: be strict so mistakes are caught during testing.
        raise ValueError(f"Unknown cooldown channel: {channel}")


    def _compute_fetch_cooldown(
        self,
        *,
        kind: FetchFailureKind,
        fail_count: int,
        retry_after_seconds: Optional[float],
    ) -> float:
        # Tunables (env vars). Safe defaults for Cloud Run + SmartAPI.
        base = float(os.getenv("ANGEL_FETCH_COOLDOWN_BASE_SECONDS", "5"))
        max_delay = float(os.getenv("ANGEL_FETCH_COOLDOWN_MAX_SECONDS", "120"))
        rate_floor = float(os.getenv("ANGEL_FETCH_RATE_LIMIT_FLOOR_SECONDS", "60"))
        blocked_floor = float(os.getenv("ANGEL_FETCH_BLOCKED_FLOOR_SECONDS", "30"))
        timeout_cap = float(os.getenv("ANGEL_FETCH_TIMEOUT_CAP_SECONDS", "60"))

        if retry_after_seconds is not None and retry_after_seconds > 0:
            delay = float(retry_after_seconds)
        else:
            if kind == FetchFailureKind.RATE_LIMIT:
                delay = rate_floor
            elif kind == FetchFailureKind.BLOCKED:
                delay = blocked_floor
            elif kind == FetchFailureKind.TIMEOUT:
                delay = min(timeout_cap, base * (2 ** max(0, fail_count - 1)))
            else:
                delay = max(2.0, (base / 2.0)) * (2 ** max(0, fail_count - 1))

        delay = min(max_delay, delay)
        # Small jitter avoids thundering herds when multiple tenants poll at the same time.
        delay *= 1.0 + random.uniform(0.0, 0.2)
        return delay

    @staticmethod
    def _schema_check_mode() -> str:
        return load_stability_feature_flags().broker_schema_check_mode

    @staticmethod
    def _order_place_timeout_safe_mode_enabled() -> bool:
        return load_stability_feature_flags().order_place_timeout_safe_mode

    def _blocked_order_response(
        self,
        *,
        message: str,
        retry_after_seconds: Optional[float] = None,
        failure_kind: str = "blocked",
    ) -> OrderResponse:
        self._orders_blocked_count += 1
        cooldown_seconds = min(
            300.0,
            max(
                float(retry_after_seconds or 0.0),
                5.0 * (2 ** max(0, self._orders_blocked_count - 1)),
            ),
        )
        self._orders_blocked_until = time.monotonic() + cooldown_seconds
        log_event(
            logger,
            event_type="ORDER_BLOCKED",
            message=f"Angel order blocked: {message}",
            tenant_id=self._tenant_id,
            broker_account_id=self._broker_account_id,
            level=logging.ERROR,
            cooldown_seconds=cooldown_seconds,
            failure_kind=failure_kind,
        )
        return OrderResponse(
            broker_order_id="BLOCKED",
            status="REJECTED",
            message=message,
            filled_quantity=0,
            average_price=None,
        )

    @staticmethod
    def _tenant_cooldown_local_clear_only_enabled() -> bool:
        return load_stability_feature_flags().tenant_cooldown_local_clear_only

    @staticmethod
    def _preserve_terminal_order_snapshot_enabled() -> bool:
        return load_stability_feature_flags().order_snapshot_preserve_terminal

    @staticmethod
    def _normalize_order_status(value: object) -> str:
        raw = str(value or "").strip()
        upper = raw.upper()
        if upper == "COMPLETED":
            return "COMPLETE"
        if upper == "CANCELED":
            return "CANCELLED"
        return upper

    def _require_float(
        self,
        data: dict[str, Any],
        keys: list[str],
        *,
        field: str,
    ) -> float:
        for key in keys:
            if key not in data:
                continue
            value = self._coerce_float(data.get(key))
            if value is not None:
                return float(value)
        raise BrokerSchemaError(
            field=field,
            keys=keys,
            payload_snippet=_safe_snippet(data),
        )

    # Angel One RMS API returns component-level utilised fields rather than a
    # single rolled-up utilisedmargin.  Sum them to get total utilized margin.
    _ANGEL_UTILISED_COMPONENT_KEYS: tuple[str, ...] = (
        "utilisedspan",
        "utilisedoptionpremium",
        "utilisedexposure",
        "utiliseddebits",
        "utilisedturnover",
        "utilisedpayout",
    )

    def _sum_angel_utilised_components(self, data: dict[str, Any]) -> Optional[float]:
        """Return sum of Angel's component utilised fields if any are present, else None."""
        if not any(k in data for k in self._ANGEL_UTILISED_COMPONENT_KEYS):
            return None
        return sum(
            self._coerce_float(data.get(k)) or 0.0
            for k in self._ANGEL_UTILISED_COMPONENT_KEYS
        )

    def _parse_balance_values(
        self,
        data: dict[str, Any],
        *,
        schema_mode: str,
    ) -> tuple[float, float, float]:
        if schema_mode == "off":
            available = (
                self._coerce_float(
                    data.get("availablecash")
                    or data.get("available")
                    or data.get("available_cash")
                )
                or 0.0
            )
            total = (
                self._coerce_float(
                    data.get("net") or data.get("total") or data.get("totalcash")
                )
                or 0.0
            )
            utilized = (
                self._coerce_float(
                    data.get("utilisedmargin")
                    or data.get("utilized")
                    or data.get("utilised")
                    or data.get("used")
                )
                or self._sum_angel_utilised_components(data)
                or 0.0
            )
            return float(available), float(total), float(utilized)

        available = self._require_float(
            data,
            ["availablecash", "available", "available_cash"],
            field="balance.available",
        )
        total = self._require_float(
            data,
            ["net", "total", "totalcash"],
            field="balance.total",
        )
        # Try primary rolled-up key first; fall back to summing Angel's component fields.
        primary_keys = ["utilisedmargin", "utilized", "utilised", "used"]
        try:
            utilized = self._require_float(data, primary_keys, field="balance.utilized")
        except BrokerSchemaError:
            component_total = self._sum_angel_utilised_components(data)
            if component_total is None:
                raise
            logger.debug(
                "balance.utilized: primary keys %s absent; computed from Angel component fields: %.2f",
                primary_keys,
                component_total,
            )
            utilized = component_total
        return available, total, utilized

    async def get_balance(self) -> Balance:
        if self._order_client is None:
            await self.login()
        assert self._order_client is not None, "AngelBrokerClient not logged in"

        remaining = self._cooldown_remaining(self._balance_fetch_cooldown_until, channel="balance")
        if remaining > 0:
            log_event(
                logger,
                event_type="BALANCE_FETCH_COOLDOWN",
                message=f"Balance fetch skipped due to cooldown ({remaining:.1f}s remaining).",
                tenant_id=self._tenant_id,
                broker_account_id=self._broker_account_id,
                level=logging.WARNING,
            )
            raise AngelBrokerError(f"balance fetch cooldown active ({remaining:.1f}s)")

        try:
            rms = await self._call(self._order_client.get_rms)
        except Exception as exc:
            failure = _classify_fetch_exception(exc)

            # Only re-login for likely auth/session problems.
            if failure.kind == FetchFailureKind.AUTH:
                log_event(
                    logger,
                    event_type="BALANCE_AUTH_RELOGIN",
                    message=f"Balance fetch looks like auth error; re-login then retry once: {failure.message}",
                    tenant_id=self._tenant_id,
                    broker_account_id=self._broker_account_id,
                    level=logging.WARNING,
                )
                await self._login(force=True)
                try:
                    rms = await self._call(self._order_client.get_rms)
                except Exception as exc2:
                    failure2 = _classify_fetch_exception(exc2)
                    self._balance_fail_count += 1
                    delay = self._compute_fetch_cooldown(
                        kind=failure2.kind,
                        fail_count=self._balance_fail_count,
                        retry_after_seconds=failure2.retry_after_seconds,
                    )
                    self._set_cooldown(channel="balance", seconds=delay)
                    log_event(
                        logger,
                        event_type="BALANCE_FETCH_DEFERRED",
                        message=f"Balance fetch failed after relogin; kind={failure2.kind} cooldown={delay:.1f}s err={failure2.message}",
                        tenant_id=self._tenant_id,
                        broker_account_id=self._broker_account_id,
                        level=logging.ERROR
                        if failure2.kind in (FetchFailureKind.BLOCKED, FetchFailureKind.RATE_LIMIT)
                        else logging.WARNING,
                    )
                    if failure2.kind in (FetchFailureKind.BLOCKED, FetchFailureKind.RATE_LIMIT):
                        raise AngelBlockedResponseError(
                            f"balance: {failure2.kind}: {failure2.message}"
                        ) from exc2
                    raise AngelBrokerError(
                        f"balance fetch failed after relogin: {failure2.kind}: {failure2.message}"
                    ) from exc2

                # Success after relogin
                self._balance_fail_count = 0
                self._clear_cooldown(channel="balance")
            else:
                # Non-auth failures: do NOT relogin. Apply cooldown and raise.
                self._balance_fail_count += 1
                delay = self._compute_fetch_cooldown(
                    kind=failure.kind,
                    fail_count=self._balance_fail_count,
                    retry_after_seconds=failure.retry_after_seconds,
                )
                self._set_cooldown(channel="balance", seconds=delay)
                log_event(
                    logger,
                    event_type="BALANCE_FETCH_DEFERRED",
                    message=f"Balance fetch failed; kind={failure.kind} cooldown={delay:.1f}s err={failure.message}",
                    tenant_id=self._tenant_id,
                    broker_account_id=self._broker_account_id,
                    level=logging.ERROR
                    if failure.kind in (FetchFailureKind.BLOCKED, FetchFailureKind.RATE_LIMIT)
                    else logging.WARNING,
                )
                if failure.kind in (FetchFailureKind.BLOCKED, FetchFailureKind.RATE_LIMIT):
                    raise AngelBlockedResponseError(
                        f"balance: {failure.kind}: {failure.message}"
                    ) from exc
                raise AngelBrokerError(
                    f"balance fetch failed: {failure.kind}: {failure.message}"
                ) from exc

        # Success on first try
        self._balance_fail_count = 0
        self._clear_cooldown(channel="balance")
        data = {}
        if isinstance(rms, dict):
            payload = rms.get("data")
            if isinstance(payload, dict):
                data = payload
            else:
                data = rms
        schema_mode = self._schema_check_mode()
        try:
            available, total, utilized = self._parse_balance_values(
                data,
                schema_mode=schema_mode,
            )
        except BrokerSchemaError as schema_exc:
            self._balance_fail_count += 1
            self._balance_schema_violation_count += 1
            global _global_balance_schema_violation_count
            _global_balance_schema_violation_count += 1
            delay = self._compute_fetch_cooldown(
                kind=FetchFailureKind.OTHER,
                fail_count=self._balance_fail_count,
                retry_after_seconds=None,
            )
            self._set_cooldown(channel="balance", seconds=delay)
            log_event(
                logger,
                event_type="BROKER_SCHEMA_VIOLATION",
                message=(
                    "Broker balance schema validation failed"
                    if schema_mode == "warn"
                    else "Broker balance schema validation failed (strict mode)"
                ),
                tenant_id=self._tenant_id,
                broker_account_id=self._broker_account_id,
                level=logging.CRITICAL,
                mode=schema_mode,
                field=schema_exc.field,
                keys=list(schema_exc.keys),
                payload=schema_exc.payload_snippet,
            )
            raise AngelBrokerError(
                f"balance schema violation ({schema_mode}): {schema_exc}"
            ) from schema_exc
        bal = Balance(available=available, utilized=utilized, total=total)
        self._last_balance = bal
        return bal

    @staticmethod
    def _coerce_float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_int(value: Any) -> int:
        try:
            if value is None:
                return 0
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _parse_product_type(value: Any) -> ProductType:
        raw = str(value or "INTRADAY").upper().replace(" ", "_")
        if raw == "CARRYFORWARD":
            raw = "CARRY_FORWARD"
        try:
            return ProductType(raw)
        except Exception:
            return ProductType.INTRADAY

    def _extract_positions(self, raw: Any, *, schema_mode: str = "off") -> List[Position]:
        if isinstance(raw, dict):
            positions = raw.get("data") or []
        else:
            positions = raw or []
        if not isinstance(positions, list):
            raise AngelPositionsFetchError("positions: invalid payload")
        results: List[Position] = []
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            qty_raw = pos.get("netqty")
            if qty_raw in (None, ""):
                qty_raw = pos.get("net")
            qty_float = self._coerce_float(qty_raw)
            if qty_float is None:
                if schema_mode in {"warn", "strict"}:
                    raise BrokerSchemaError(
                        field="positions.quantity",
                        keys=("netqty", "net"),
                        payload_snippet=_safe_snippet(pos),
                    )
                qty = 0
            else:
                qty = int(qty_float)
            if qty == 0:
                continue
            symbol = str(pos.get("tradingsymbol") or pos.get("symbol") or "").strip()
            if not symbol:
                continue
            # Angel position payloads are inconsistent across products and
            # can use different keys. In many cases avgprice is 0 but a
            # net/buy/sell average price is present. We treat 0 as "missing".
            def _pick_price(*values: Any) -> Optional[float]:
                for v in values:
                    f = self._coerce_float(v)
                    if f is None:
                        continue
                    if f == 0.0:
                        continue
                    return f
                return None

            avg_price = _pick_price(
                pos.get("avgprice"),
                pos.get("avgPrice"),
                pos.get("average_price"),
                pos.get("averageprice"),
                # Some brokers use netprice/netavgprice for the net position.
                pos.get("netprice"),
                pos.get("netPrice"),
                pos.get("netavgprice"),
                pos.get("netAvgPrice"),
            )
            if avg_price is None:
                # Fallback: buy/sell average price depending on net direction
                if qty > 0:
                    avg_price = _pick_price(
                        pos.get("buyavgprice"),
                        pos.get("buyAvgPrice"),
                        pos.get("buyaverageprice"),
                        pos.get("buyAveragePrice"),
                    )
                elif qty < 0:
                    avg_price = _pick_price(
                        pos.get("sellavgprice"),
                        pos.get("sellAvgPrice"),
                        pos.get("sellaverageprice"),
                        pos.get("sellAveragePrice"),
                    )
            if avg_price is None and schema_mode in {"warn", "strict"}:
                raise BrokerSchemaError(
                    field="positions.avg_price",
                    keys=(
                        "avgprice",
                        "avgPrice",
                        "average_price",
                        "averageprice",
                        "netprice",
                        "netPrice",
                        "netavgprice",
                        "netAvgPrice",
                        "buyavgprice",
                        "buyAvgPrice",
                        "sellavgprice",
                        "sellAvgPrice",
                    ),
                    payload_snippet=_safe_snippet(pos),
                )
            avg_price = float(avg_price or 0.0)
            product_type = self._parse_product_type(
                pos.get("producttype") or pos.get("productType")
            )
            results.append(
                Position(
                    symbol=symbol,
                    quantity=qty,
                    avg_price=avg_price,
                    product_type=product_type,
                )
            )
        return results

    def _parse_orders(self, raw: Any) -> List[OrderStatus]:
        if isinstance(raw, dict):
            orders = raw.get("data") or []
        else:
            orders = raw or []
        preserve_terminal = self._preserve_terminal_order_snapshot_enabled()
        results: List[OrderStatus] = []
        status_counts: dict[str, int] = {}
        terminal_preserved = 0
        terminal_dropped = 0
        for order in orders:
            if not isinstance(order, dict):
                continue
            status_raw = str(order.get("orderstatus") or order.get("status") or "").strip()
            status_norm = self._normalize_order_status(status_raw)
            status_counts[status_norm or "UNKNOWN"] = int(
                status_counts.get(status_norm or "UNKNOWN", 0)
            ) + 1
            if status_norm in _TERMINAL_ORDER_STATUSES and not preserve_terminal:
                terminal_dropped += 1
                continue
            symbol = str(order.get("tradingsymbol") or order.get("symbol") or "").strip()
            if not symbol:
                continue
            if status_norm in _TERMINAL_ORDER_STATUSES:
                terminal_preserved += 1
            order_id = str(
                order.get("orderid")
                or order.get("order_id")
                or order.get("uniqueorderid")
                or ""
            ).strip()
            side = str(order.get("transactiontype") or order.get("side") or "").upper()
            order_type = str(order.get("ordertype") or order.get("order_type") or "").upper()
            product_type = str(order.get("producttype") or order.get("product_type") or "").upper()
            variety = str(order.get("variety") or "").upper() or None
            qty = self._coerce_int(order.get("quantity") or order.get("qty"))
            filled = self._coerce_int(
                order.get("filledshares")
                or order.get("filled_qty")
                or order.get("filled_quantity")
            )
            avg_price = self._coerce_float(
                order.get("averageprice") or order.get("average_price") or order.get("avgprice")
            )
            price = self._coerce_float(order.get("price"))
            exchange = str(order.get("exchange") or "").strip() or None
            updated_at = str(
                order.get("updatetime") or order.get("exchorderupdatetime") or ""
            ).strip() or None
            # Issue #224: capture broker-side rejection text + error code
            # so the lifecycle log on the snapshot polling path can
            # surface diagnostics for REJECTED / CANCELLED orders. Angel
            # returns these as ``text`` and ``errorcode`` (sometimes
            # ``rejectionreason`` for legacy responses).
            #
            # PR #235 review (Codex P2): the previous ``or`` chain was
            # broken — a whitespace-only ``text=" "`` was selected by
            # ``or`` (truthy as a string) BEFORE trimming, then the
            # whole expression collapsed to None when stripped, dropping
            # any actual rejection text in subsequent fallback fields.
            # Fix: trim/test each candidate before choosing the first
            # non-empty value. Also include ``errorCode`` (camelCase)
            # which is used elsewhere in the codebase for Angel
            # payloads (see app/core/angel_login.py).
            def _first_non_empty(*candidates):
                for c in candidates:
                    if c is None:
                        continue
                    s = str(c).strip()
                    if s:
                        return s
                return None

            status_message = _first_non_empty(
                order.get("text"),
                order.get("rejectionreason"),
                order.get("rejection_reason"),
                order.get("status_message"),
                order.get("statusmessage"),
            )
            error_code = _first_non_empty(
                order.get("errorcode"),
                order.get("errorCode"),
                order.get("error_code"),
            )
            results.append(
                OrderStatus(
                    order_id=order_id,
                    symbol=symbol,
                    side=side,
                    status=status_norm or self._normalize_order_status(status_raw),
                    order_type=order_type,
                    product_type=product_type,
                    quantity=qty,
                    filled_quantity=filled,
                    average_price=avg_price,
                    price=price,
                    exchange=exchange,
                    variety=variety,
                    updated_at=updated_at,
                    status_message=status_message,
                    error_code=error_code,
                )
            )
        log_event(
            logger,
            event_type="ORDERS_SNAPSHOT_PARSED",
            message="Parsed Angel order snapshot.",
            tenant_id=self._tenant_id,
            broker_account_id=self._broker_account_id,
            level=logging.DEBUG,
            preserve_terminal_mode=preserve_terminal,
            rows_total=len(orders),
            rows_emitted=len(results),
            terminal_rows_preserved=terminal_preserved,
            terminal_rows_dropped=terminal_dropped,
            status_counts=json.dumps(status_counts, sort_keys=True),
        )
        return results

    def _compute_positions_retry_after(
        self,
        *,
        reason: str,
        retry_after_seconds: Optional[float],
    ) -> float:
        self._positions_blocked_count += 1
        if retry_after_seconds is not None and retry_after_seconds > 0:
            delay = float(retry_after_seconds)
        else:
            delay = min(300.0, 5.0 * (2 ** (self._positions_blocked_count - 1)))
            delay *= 1.0 + random.uniform(0.0, 0.2)
        if reason == "waf_rejected":
            delay = max(delay, 30.0)
        return delay

    def _blocked_positions_result(
        self,
        *,
        reason: str,
        http_status: Optional[int],
        retry_after_seconds: Optional[float],
        raw_snippet: Optional[str],
    ) -> PositionsFetchResult:
        delay = self._compute_positions_retry_after(
            reason=reason, retry_after_seconds=retry_after_seconds
        )
        return PositionsFetchResult(
            status=PositionsStatus.BLOCKED,
            positions=None,
            reason=reason,
            http_status=http_status,
            retry_after_seconds=delay,
            raw_snippet=raw_snippet,
        )

    @staticmethod
    def _error_positions_result(
        *,
        reason: str,
        http_status: Optional[int],
        raw_snippet: Optional[str],
    ) -> PositionsFetchResult:
        return PositionsFetchResult(
            status=PositionsStatus.ERROR,
            positions=None,
            reason=reason,
            http_status=http_status,
            raw_snippet=raw_snippet,
        )

    def _positions_result_from_raw(self, raw: Any) -> PositionsFetchResult:
        schema_mode = self._schema_check_mode()
        if raw is None:
            return self._error_positions_result(
                reason="empty_body", http_status=None, raw_snippet=None
            )
        if isinstance(raw, (bytes, str)):
            text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else raw
            if not text.strip():
                return self._error_positions_result(
                    reason="empty_body", http_status=None, raw_snippet=None
                )
            if is_html_block(text, None):
                reason = "waf_rejected" if "request rejected" in text.lower() else "html_blocked"
                return self._blocked_positions_result(
                    reason=reason,
                    http_status=200,
                    retry_after_seconds=None,
                    raw_snippet=_safe_snippet(text),
                )
            return self._error_positions_result(
                reason="parse_error", http_status=None, raw_snippet=_safe_snippet(text)
            )
        if isinstance(raw, dict):
            data = raw.get("data")
            if isinstance(data, (bytes, str)):
                text = data.decode("utf-8", errors="ignore") if isinstance(data, bytes) else data
                if not text.strip():
                    return self._error_positions_result(
                        reason="empty_body", http_status=None, raw_snippet=None
                    )
                if is_html_block(text, None):
                    reason = "waf_rejected" if "request rejected" in text.lower() else "html_blocked"
                    return self._blocked_positions_result(
                        reason=reason,
                        http_status=200,
                        retry_after_seconds=None,
                        raw_snippet=_safe_snippet(text),
                    )
            status_val = raw.get("status")
            if status_val is False or (
                isinstance(status_val, str) and status_val.strip().lower() == "false"
            ):
                message = str(raw.get("message") or raw.get("errorcode") or "")
                if _is_rate_limited_message(message):
                    return self._blocked_positions_result(
                        reason="rate_limited",
                        http_status=None,
                        retry_after_seconds=None,
                        raw_snippet=_safe_snippet(message),
                    )
                return self._error_positions_result(
                    reason="status_false",
                    http_status=None,
                    raw_snippet=_safe_snippet(message or raw),
                )
            try:
                positions = self._extract_positions(raw, schema_mode=schema_mode)
            except BrokerSchemaError as exc:
                if schema_mode == "strict":
                    raise
                log_event(
                    logger,
                    event_type="BROKER_SCHEMA_VIOLATION",
                    message="Broker positions schema validation failed",
                    tenant_id=self._tenant_id,
                    broker_account_id=self._broker_account_id,
                    level=logging.CRITICAL,
                    mode=schema_mode,
                    field=exc.field,
                    keys=list(exc.keys),
                    payload=exc.payload_snippet,
                )
                return self._error_positions_result(
                    reason="schema_violation",
                    http_status=None,
                    raw_snippet=exc.payload_snippet,
                )
            except Exception:
                return self._error_positions_result(
                    reason="parse_error",
                    http_status=None,
                    raw_snippet=_safe_snippet(raw),
                )
            return PositionsFetchResult(
                status=PositionsStatus.OK,
                positions=positions,
                reason="ok",
            )
        if isinstance(raw, (list, tuple)):
            try:
                positions = self._extract_positions(raw, schema_mode=schema_mode)
            except BrokerSchemaError as exc:
                if schema_mode == "strict":
                    raise
                log_event(
                    logger,
                    event_type="BROKER_SCHEMA_VIOLATION",
                    message="Broker positions schema validation failed",
                    tenant_id=self._tenant_id,
                    broker_account_id=self._broker_account_id,
                    level=logging.CRITICAL,
                    mode=schema_mode,
                    field=exc.field,
                    keys=list(exc.keys),
                    payload=exc.payload_snippet,
                )
                return self._error_positions_result(
                    reason="schema_violation",
                    http_status=None,
                    raw_snippet=exc.payload_snippet,
                )
            except Exception:
                return self._error_positions_result(
                    reason="parse_error",
                    http_status=None,
                    raw_snippet=_safe_snippet(raw),
                )
            return PositionsFetchResult(
                status=PositionsStatus.OK,
                positions=positions,
                reason="ok",
            )
        return self._error_positions_result(
            reason="invalid_payload", http_status=None, raw_snippet=_safe_snippet(raw)
        )

    def _positions_result_from_exception(self, exc: Exception) -> PositionsFetchResult:
        http_status = getattr(exc, "status", None)
        content_type = getattr(exc, "content_type", None)
        body_head = getattr(exc, "body_head", None)
        retry_after = getattr(exc, "retry_after_seconds", None)
        snippet_source = body_head or str(exc)
        snippet = _safe_snippet(snippet_source)
        text_for_reason = str(snippet_source or "").lower()
        if http_status == 403 and _is_rate_limited_message(text_for_reason):
            return self._blocked_positions_result(
                reason="rate_limited",
                http_status=http_status,
                retry_after_seconds=retry_after,
                raw_snippet=snippet,
            )
        if is_html_block(snippet_source, content_type):
            reason = "waf_rejected" if "request rejected" in text_for_reason else "html_blocked"
            return self._blocked_positions_result(
                reason=reason,
                http_status=http_status,
                retry_after_seconds=retry_after,
                raw_snippet=snippet,
            )
        if isinstance(exc, core_order_client.AngelHTTPBlockedError):
            return self._blocked_positions_result(
                reason="blocked",
                http_status=http_status,
                retry_after_seconds=retry_after,
                raw_snippet=snippet,
            )
        return self._error_positions_result(
            reason="exception", http_status=http_status, raw_snippet=snippet
        )

    async def get_positions(self) -> PositionsFetchResult:
        if self._order_client is None:
            await self.login()
        assert self._order_client is not None, "AngelBrokerClient not logged in"

        remaining = self._cooldown_remaining(self._positions_fetch_cooldown_until, channel="positions")
        if remaining > 0:
            log_event(
                logger,
                event_type="POSITIONS_FETCH_COOLDOWN",
                message=f"Positions fetch skipped due to cooldown ({remaining:.1f}s remaining).",
                tenant_id=self._tenant_id,
                broker_account_id=self._broker_account_id,
                level=logging.WARNING,
            )
            return PositionsFetchResult(
                status=PositionsStatus.BLOCKED,
                positions=None,
                reason="cooldown",
                http_status=None,
                retry_after_seconds=remaining,
                raw_snippet=None,
            )

        last_result: Optional[PositionsFetchResult] = None

        def _looks_auth_from_result(res: PositionsFetchResult) -> bool:
            if res.http_status in (401, 403) and res.reason != "rate_limited":
                return True
            snippet = (res.raw_snippet or res.reason or "").lower()
            return _looks_like_auth_error(snippet)

        try:
            raw = await self._call(self._order_client.get_positions)
            result = self._positions_result_from_raw(raw)
            last_result = result

            if result.status == PositionsStatus.OK:
                self._positions_fail_count = 0
                self._clear_cooldown(channel="positions")
                self._positions_blocked_count = 0
                self._last_positions_result = result
                return result

            if result.status == PositionsStatus.BLOCKED:
                # Respect retry_after from result (already computed with exponential backoff).
                delay = float(result.retry_after_seconds or 0.0)
                if delay > 0:
                    self._set_cooldown(channel="positions", seconds=delay)
                if result.reason == "waf_rejected":
                    log_event(
                        logger,
                        event_type="BROKER_WAF_BLOCK",
                        message=(
                            f"Broker positions endpoint blocked by WAF; "
                            f"cooldown={delay:.0f}s snippet={result.raw_snippet or '-'}"
                        ),
                        tenant_id=self._tenant_id,
                        broker_account_id=self._broker_account_id,
                        level=logging.CRITICAL,
                        endpoint="positions",
                        cooldown_seconds=delay,
                    )
                self._last_positions_result = result
                return result

            # ERROR: decide whether to relogin based on content (avoid relogin storms).
            if _looks_auth_from_result(result):
                log_event(
                    logger,
                    event_type="POSITIONS_AUTH_RELOGIN",
                    message=f"Positions fetch looks like auth error; re-login then retry once: reason={result.reason} snippet={result.raw_snippet or '-'}",
                    tenant_id=self._tenant_id,
                    broker_account_id=self._broker_account_id,
                    level=logging.WARNING,
                )
                await self._login(force=True)
                try:
                    raw2 = await self._call(self._order_client.get_positions)
                    result2 = self._positions_result_from_raw(raw2)
                    last_result = result2
                    if result2.status == PositionsStatus.OK:
                        self._positions_fail_count = 0
                        self._clear_cooldown(channel="positions")
                        self._positions_blocked_count = 0
                        self._last_positions_result = result2
                    elif result2.status == PositionsStatus.BLOCKED:
                        delay2 = float(result2.retry_after_seconds or 0.0)
                        if delay2 > 0:
                            self._set_cooldown(channel="positions", seconds=delay2)
                        self._last_positions_result = result2
                    else:
                        self._positions_fail_count += 1
                        delay2 = self._compute_fetch_cooldown(
                            kind=FetchFailureKind.OTHER,
                            fail_count=self._positions_fail_count,
                            retry_after_seconds=None,
                        )
                        self._set_cooldown(channel="positions", seconds=delay2)
                    return result2
                except Exception as exc2:
                    failure2 = _classify_fetch_exception(exc2)
                    self._positions_fail_count += 1
                    delay2 = self._compute_fetch_cooldown(
                        kind=failure2.kind,
                        fail_count=self._positions_fail_count,
                        retry_after_seconds=failure2.retry_after_seconds,
                    )
                    self._set_cooldown(channel="positions", seconds=delay2)
                    log_event(
                        logger,
                        event_type="POSITIONS_FETCH_DEFERRED",
                        message=f"Positions fetch failed after relogin; kind={failure2.kind} cooldown={delay2:.1f}s err={failure2.message}",
                        tenant_id=self._tenant_id,
                        broker_account_id=self._broker_account_id,
                        level=logging.ERROR
                        if failure2.kind in (FetchFailureKind.BLOCKED, FetchFailureKind.RATE_LIMIT)
                        else logging.WARNING,
                    )
                    return self._positions_result_from_exception(exc2)

            # Non-auth ERROR: cooldown and return the error result (AccountRunner will keep last known positions).
            self._positions_fail_count += 1
            delay = self._compute_fetch_cooldown(
                kind=FetchFailureKind.OTHER,
                fail_count=self._positions_fail_count,
                retry_after_seconds=None,
            )
            self._set_cooldown(channel="positions", seconds=delay)
            log_event(
                logger,
                event_type="POSITIONS_FETCH_DEFERRED",
                message=f"Positions fetch error; cooldown={delay:.1f}s reason={result.reason} snippet={result.raw_snippet or '-'}",
                tenant_id=self._tenant_id,
                broker_account_id=self._broker_account_id,
                level=logging.WARNING,
            )
            return result

        except BrokerSchemaError as schema_exc:
            self._positions_fail_count += 1
            delay = self._compute_fetch_cooldown(
                kind=FetchFailureKind.OTHER,
                fail_count=self._positions_fail_count,
                retry_after_seconds=None,
            )
            self._set_cooldown(channel="positions", seconds=delay)
            log_event(
                logger,
                event_type="BROKER_SCHEMA_VIOLATION",
                message="Broker positions schema validation failed (strict mode)",
                tenant_id=self._tenant_id,
                broker_account_id=self._broker_account_id,
                level=logging.CRITICAL,
                mode="strict",
                field=schema_exc.field,
                keys=list(schema_exc.keys),
                payload=schema_exc.payload_snippet,
            )
            return PositionsFetchResult(
                status=PositionsStatus.BLOCKED,
                positions=None,
                reason="schema_violation",
                http_status=None,
                retry_after_seconds=delay,
                raw_snippet=schema_exc.payload_snippet,
            )
        except Exception as exc:
            # Exception path: classify; relogin only on auth; otherwise cooldown and return error result.
            failure = _classify_fetch_exception(exc)

            if failure.kind == FetchFailureKind.AUTH:
                log_event(
                    logger,
                    event_type="POSITIONS_AUTH_RELOGIN",
                    message=f"Positions fetch raised auth-like error; re-login then retry once: {failure.message}",
                    tenant_id=self._tenant_id,
                    broker_account_id=self._broker_account_id,
                    level=logging.WARNING,
                )
                try:
                    await self._login(force=True)
                except Exception as relogin_exc:
                    log_event(
                        logger,
                        event_type="POSITIONS_RELOGIN_FAILED",
                        message=f"Angel relogin failed after position error: {relogin_exc}",
                        tenant_id=self._tenant_id,
                        broker_account_id=self._broker_account_id,
                        level=logging.ERROR,
                    )
                    if last_result is not None:
                        return last_result
                    return self._error_positions_result(
                        reason="exception",
                        http_status=None,
                        raw_snippet=_safe_snippet(relogin_exc),
                    )
                try:
                    raw2 = await self._call(self._order_client.get_positions)
                    result2 = self._positions_result_from_raw(raw2)
                    if result2.status == PositionsStatus.OK:
                        self._positions_fail_count = 0
                        self._clear_cooldown(channel="positions")
                        self._positions_blocked_count = 0
                        self._last_positions_result = result2
                    elif result2.status == PositionsStatus.BLOCKED:
                        delay2 = float(result2.retry_after_seconds or 0.0)
                        if delay2 > 0:
                            self._set_cooldown(channel="positions", seconds=delay2)
                        self._last_positions_result = result2
                    return result2
                except Exception as exc2:
                    failure2 = _classify_fetch_exception(exc2)
                    self._positions_fail_count += 1
                    delay2 = self._compute_fetch_cooldown(
                        kind=failure2.kind,
                        fail_count=self._positions_fail_count,
                        retry_after_seconds=failure2.retry_after_seconds,
                    )
                    self._set_cooldown(channel="positions", seconds=delay2)
                    return self._positions_result_from_exception(exc2)

            # Non-auth exception: cooldown; return parsed exception result.
            self._positions_fail_count += 1
            delay = self._compute_fetch_cooldown(
                kind=failure.kind,
                fail_count=self._positions_fail_count,
                retry_after_seconds=failure.retry_after_seconds,
            )
            self._set_cooldown(channel="positions", seconds=delay)
            log_event(
                logger,
                event_type="POSITIONS_FETCH_DEFERRED",
                message=f"Positions fetch exception; kind={failure.kind} cooldown={delay:.1f}s err={failure.message}",
                tenant_id=self._tenant_id,
                broker_account_id=self._broker_account_id,
                level=logging.ERROR
                if failure.kind in (FetchFailureKind.BLOCKED, FetchFailureKind.RATE_LIMIT)
                else logging.WARNING,
            )
            return self._positions_result_from_exception(exc)


    async def get_orders(self) -> List[OrderStatus]:
        if self._order_client is None:
            await self.login()
        assert self._order_client is not None, "AngelBrokerClient not logged in"

        remaining = self._cooldown_remaining(self._orders_fetch_cooldown_until, channel="orders")
        if remaining > 0:
            # Instead of raising, return cached orders if available (Fix #3)
            now_mono = time.monotonic()
            if self._order_cache is not None and (now_mono - self._order_cache_time) <= self._order_cache_ttl:
                log_event(
                    logger,
                    event_type="ORDERS_FETCH_COOLDOWN_CACHED",
                    message=f"Order book fetch on cooldown; returning cached orders ({remaining:.1f}s remaining).",
                    tenant_id=self._tenant_id,
                    broker_account_id=self._broker_account_id,
                    level=logging.DEBUG,
                )
                return self._order_cache
            
            log_event(
                logger,
                event_type="ORDERS_FETCH_COOLDOWN",
                message=f"Order book fetch skipped due to cooldown ({remaining:.1f}s remaining) and no valid cache.",
                tenant_id=self._tenant_id,
                broker_account_id=self._broker_account_id,
                level=logging.WARNING,
            )
            raise AngelBrokerError(f"orders fetch cooldown active ({remaining:.1f}s)")

        try:
            raw = await self._call(self._order_client.get_order_book)
            orders = self._parse_orders(raw)
            self._last_orders = orders
            # Cache fresh orders for use during cooldowns
            self._order_cache = orders
            self._order_cache_time = time.monotonic()
            self._orders_fail_count = 0
            self._clear_cooldown(channel="orders")
            return orders
        except Exception as exc:
            failure = _classify_fetch_exception(exc)

            # Only re-login for likely auth/session problems.
            if failure.kind == FetchFailureKind.AUTH:
                log_event(
                    logger,
                    event_type="ORDERS_AUTH_RELOGIN",
                    message=f"Order book fetch looks like auth error; re-login then retry once: {failure.message}",
                    tenant_id=self._tenant_id,
                    broker_account_id=self._broker_account_id,
                    level=logging.WARNING,
                )
                await self._login(force=True)
                try:
                    raw = await self._call(self._order_client.get_order_book)
                    orders = self._parse_orders(raw)
                    self._last_orders = orders
                    # Cache fresh orders for use during cooldowns
                    self._order_cache = orders
                    self._order_cache_time = time.monotonic()
                    self._orders_fail_count = 0
                    self._clear_cooldown(channel="orders")
                    return orders
                except Exception as exc2:
                    failure2 = _classify_fetch_exception(exc2)
                    self._orders_fail_count += 1
                    delay = self._compute_fetch_cooldown(
                        kind=failure2.kind,
                        fail_count=self._orders_fail_count,
                        retry_after_seconds=failure2.retry_after_seconds,
                    )
                    self._set_cooldown(channel="orders", seconds=delay)
                    log_event(
                        logger,
                        event_type="ORDERS_FETCH_DEFERRED",
                        message=f"Order book fetch failed after relogin; kind={failure2.kind} cooldown={delay:.1f}s err={failure2.message}",
                        tenant_id=self._tenant_id,
                        broker_account_id=self._broker_account_id,
                        level=logging.ERROR
                        if failure2.kind in (FetchFailureKind.BLOCKED, FetchFailureKind.RATE_LIMIT)
                        else logging.WARNING,
                    )
                    if failure2.kind in (FetchFailureKind.BLOCKED, FetchFailureKind.RATE_LIMIT):
                        raise AngelBlockedResponseError(
                            f"orders: {failure2.kind}: {failure2.message}"
                        ) from exc2
                    raise AngelBrokerError(
                        f"order book fetch failed after relogin: {failure2.kind}: {failure2.message}"
                    ) from exc2

            # Non-auth failures: do NOT relogin. Apply cooldown and raise.
            self._orders_fail_count += 1
            delay = self._compute_fetch_cooldown(
                kind=failure.kind,
                fail_count=self._orders_fail_count,
                retry_after_seconds=failure.retry_after_seconds,
            )
            self._set_cooldown(channel="orders", seconds=delay)
            log_event(
                logger,
                event_type="ORDERS_FETCH_DEFERRED",
                message=f"Order book fetch failed; kind={failure.kind} cooldown={delay:.1f}s err={failure.message}",
                tenant_id=self._tenant_id,
                broker_account_id=self._broker_account_id,
                level=logging.ERROR
                if failure.kind in (FetchFailureKind.BLOCKED, FetchFailureKind.RATE_LIMIT)
                else logging.WARNING,
            )
            if failure.kind in (FetchFailureKind.BLOCKED, FetchFailureKind.RATE_LIMIT):
                raise AngelBlockedResponseError(
                    f"orders: {failure.kind}: {failure.message}"
                ) from exc
            raise AngelBrokerError(
                f"order book fetch failed: {failure.kind}: {failure.message}"
            ) from exc

    async def cancel_order(
        self,
        broker_order_id: str,
        *,
        symbol: Optional[str] = None,
        variety: Optional[str] = None,
    ) -> OrderResponse:
        if self._order_client is None:
            await self.login()
        assert self._order_client is not None, "AngelBrokerClient not logged in"
        oid = str(broker_order_id or "").strip()
        if not oid:
            return OrderResponse(
                broker_order_id="",
                status="REJECTED",
                message="missing_order_id",
                filled_quantity=0,
                average_price=None,
            )
        try:
            raw = await self._call(
                self._order_client.cancel_order,
                order_id=oid,
                variety=str(variety or "NORMAL").upper(),
                symbol=symbol,
            )
            ok = bool((raw or {}).get("status"))
            message = str((raw or {}).get("message") or "")
            data = (raw or {}).get("data")
            cancel_id = oid
            if isinstance(data, dict):
                for key in ("orderid", "order_id"):
                    val = data.get(key)
                    if val:
                        cancel_id = str(val)
                        break
            status = "CANCELLED" if ok else "REJECTED"
            return OrderResponse(
                broker_order_id=cancel_id,
                status=status,
                message=message or ("cancelled" if ok else "cancel_failed"),
                filled_quantity=0,
                average_price=None,
            )
        except Exception as exc:
            failure = _classify_fetch_exception(exc)
            return OrderResponse(
                broker_order_id=oid,
                status="ERROR",
                message=f"cancel_failed:{failure.kind}:{failure.message}",
                filled_quantity=0,
                average_price=None,
            )


    async def place_order(self, request: OrderRequest) -> OrderResponse:
        assert self._order_client is not None, "AngelBrokerClient not logged in"
        duration = request.time_in_force.value
        product_type = request.product_type.value
        order_type = request.order_type.value
        side = request.side.value

        exchange = request.exchange or "NSE"
        symbol_token = request.symbol_token or request.symbol
        logger.debug(
            "AngelBrokerClient.place_order exchange=%s symbol=%s token=%s side=%s qty=%s",
            exchange,
            request.symbol,
            symbol_token,
            side,
            request.quantity,
        )
        now = time.monotonic()
        if now < self._orders_blocked_until:
            return OrderResponse(
                broker_order_id="BLOCKED",
                status="REJECTED",
                message="order blocked due to prior rate limit",
                filled_quantity=0,
                average_price=None,
            )
        result: Any = None
        for attempt in range(1, self._order_place_max_attempts + 1):
            try:
                result = await self._call(
                    self._order_client.place_order,
                    exchange=exchange,
                    tradingsymbol=request.symbol,
                    symboltoken=str(symbol_token),
                    transaction_type=side,
                    quantity=request.quantity,
                    producttype=product_type,
                    ordertype=order_type,
                    variety="NORMAL",
                    duration=duration,
                    price=request.limit_price,
                    trigger_price=request.stop_price,
                    tag=request.tag,
                    tick_size=request.tick_size,
                )
                self._orders_blocked_count = 0
                self._orders_blocked_until = 0.0
                break
            except Exception as exc:
                if _is_blocked_exception(exc):
                    return self._blocked_order_response(
                        message=str(exc),
                        retry_after_seconds=getattr(exc, "retry_after_seconds", None),
                    )
                failure = _classify_fetch_exception(exc)
                if failure.kind in (
                    FetchFailureKind.BLOCKED,
                    FetchFailureKind.RATE_LIMIT,
                ):
                    return self._blocked_order_response(
                        message=failure.message,
                        retry_after_seconds=failure.retry_after_seconds,
                        failure_kind=failure.kind.value,
                    )
                if (
                    failure.kind == FetchFailureKind.TIMEOUT
                    and self._order_place_timeout_safe_mode_enabled()
                ):
                    message = (
                        "place_order_timeout_pending_reconciliation:"
                        "broker placement timed out after submit attempt;"
                        " awaiting outbox/broker reconciliation"
                    )
                    log_event(
                        logger,
                        event_type="ORDER_PLACE_TIMEOUT_PENDING_RECONCILIATION",
                        message="Broker order placement timed out; pending reconciliation.",
                        tenant_id=self._tenant_id,
                        broker_account_id=self._broker_account_id,
                        level=logging.WARNING,
                        symbol=request.symbol,
                        qty=int(request.quantity or 0),
                        attempt=attempt,
                        max_attempts=self._order_place_max_attempts,
                        idempotency_key=str(
                            getattr(request, "idempotency_key", "") or ""
                        ).strip()
                        or None,
                        tag=str(getattr(request, "tag", "") or "").strip() or None,
                    )
                    return OrderResponse(
                        broker_order_id=_PENDING_TIMEOUT_BROKER_ORDER_ID,
                        status="PENDING",
                        message=message,
                        filled_quantity=0,
                        average_price=None,
                        requested_quantity=int(request.quantity or 0),
                        details={
                            "pending_reconciliation": True,
                            "reconciliation_reason": "broker_place_timeout",
                        },
                    )
                if (
                    failure.kind == FetchFailureKind.TIMEOUT
                    and attempt < self._order_place_max_attempts
                ):
                    backoff = min(
                        5.0,
                        self._order_place_retry_base_seconds * (2 ** (attempt - 1)),
                    )
                    backoff *= 1.0 + random.uniform(0.0, 0.2)
                    log_event(
                        logger,
                        event_type="ORDER_PLACE_RETRY",
                        message=(
                            f"Order place timeout; retrying in {backoff:.2f}s "
                            f"(attempt {attempt}/{self._order_place_max_attempts})"
                        ),
                        tenant_id=self._tenant_id,
                        broker_account_id=self._broker_account_id,
                        level=logging.WARNING,
                    )
                    await asyncio.sleep(backoff)
                    continue
                log_event(
                    logger,
                    event_type="ORDER_PLACE_FAILED",
                    message=(
                        f"Order place failed; kind={failure.kind} "
                        f"attempt={attempt}/{self._order_place_max_attempts} "
                        f"err={failure.message}"
                    ),
                    tenant_id=self._tenant_id,
                    broker_account_id=self._broker_account_id,
                    level=logging.ERROR
                    if failure.kind in (FetchFailureKind.BLOCKED, FetchFailureKind.RATE_LIMIT)
                    else logging.WARNING,
                )
                return OrderResponse(
                    broker_order_id="",
                    status="FAILED",
                    message=f"place_order_failed:{failure.kind}:{failure.message}",
                    filled_quantity=0,
                    average_price=None,
                )

        if result is None:
            return OrderResponse(
                broker_order_id="",
                status="FAILED",
                message="place_order_failed:unknown:no broker response",
                filled_quantity=0,
                average_price=None,
            )

        data_block = result.get("data", {}) if isinstance(result, dict) else {}
        avg_price = result.get("average_price") or data_block.get("average_price")

        # --- Normalize broker status ---
        # Angel SmartAPI may return {"status": true} (bool) for successful
        # placement.  Convert to canonical string before constructing the
        # OrderResponse so downstream classify_broker_status never sees a
        # raw bool stringified to "True".
        raw_status = result.get("status") if result.get("status") is not None else data_block.get("status")
        if isinstance(raw_status, bool):
            normalised_status = "SUCCESS" if raw_status else "FAILED"
        elif raw_status is not None:
            normalised_status = str(raw_status)
        else:
            normalised_status = "UNKNOWN"

        return OrderResponse(
            broker_order_id=str(
                result.get("orderid")
                or result.get("order_id")
                or data_block.get("orderid")
                or data_block.get("order_id")
                or ""
            ),
            status=normalised_status,
            message=str(
                result.get("message")
                or result.get("remarks")
                or data_block.get("message")
                or ""
            ),
            filled_quantity=int(
                result.get("filled_qty") or data_block.get("filled_qty") or 0
            ),
            average_price=float(avg_price) if avg_price is not None else None,
        )


__all__ = [
    "AngelBrokerClient",
    "AngelBrokerError",
    "AngelBlockedResponseError",
    "AngelPositionsFetchError",
]
