"""
Settings for the Cloud Run multi-tenant trading hub.

All env-driven configuration for the control plane (Firestore), data plane
(BigQuery), and hub feature flags flows through this module to keep deployments
consistent across tenants and broker accounts.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Optional
import logging

try:  # pragma: no cover - optional dependency in local/dev
    from google.cloud import secretmanager  # type: ignore
except Exception:  # pragma: no cover - optional dependency in local/dev
    secretmanager = None

from pydantic_settings import BaseSettings
from pydantic import Field, ConfigDict, model_validator


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatabaseSettings:
    gcp_project_id: str
    bq_dataset_id: str
    bq_orders_table: str
    bq_trades_table: str
    bq_daily_pnl_table: str
    bigquery_trades_table: str
    bigquery_pnl_table: str


@dataclass(frozen=True)
class InfrastructureSettings:
    default_time_zone: str
    firestore_collection_prefix: str
    control_plane_backend: str
    sweep_state_backend: str
    hub_instance_name: str
    hub_subscription_poll_interval: float


@dataclass(frozen=True)
class RiskGuardSettings:
    enable_capital_checks: bool
    capital_auto_resize_enabled: bool
    enable_risk_checks: bool
    risk_enable_daily_loss: bool
    risk_max_daily_loss: Optional[float]
    risk_fail_open_on_missing_pnl: bool
    risk_include_unrealized_in_daily_loss: bool
    enable_profit_checks: bool


@dataclass(frozen=True)
class StrategyFeatureFlags:
    use_hub_router: bool
    use_hub_router_for_ng_options: bool
    use_hub_router_for_nifty_options: bool
    use_hub_router_for_nifty_delta: bool
    use_hub_router_for_banknifty_options: bool
    use_hub_router_for_banknifty_delta: bool
    use_hub_router_for_finnifty_options: bool
    use_hub_router_for_sensex_options: bool
    use_hub_router_for_midcpnifty_options: bool


class Settings(BaseSettings):
    """
    Central configuration for the Cloud Run FastAPI hub.

    This object is the single source of truth for:
    - Control plane: Firestore collection prefixes and tenant/broker metadata.
    - Data plane: BigQuery dataset/table identifiers for trades, PnL, and logs.
    - Secrets/config: Secret Manager prefixes, admin API keys, feature flags.

    Add new settings here to avoid scattered environment lookups that bypass
    tenant/broker isolation or Cloud Run deployment defaults.
    """

    model_config = ConfigDict(
        env_prefix="",
        case_sensitive=False,
    )

    gcp_project_id: str = Field(
        default="",
        description="GCP project hosting Cloud Run, Firestore, and BigQuery.",
    )
    bq_dataset_id: str = Field(
        default="",
        description="BigQuery dataset for hub data (trades, pnl, logs).",
    )
    log_level: str = Field(
        default="INFO",
        description="Default log level for hub processes (e.g., uvicorn, workers).",
    )
    default_time_zone: str = Field(
        default="Asia/Kolkata",
        description="Canonical time zone for scheduling and reporting.",
    )
    admin_api_key: Optional[str] = Field(
        default=None,
        validation_alias="ADMIN_API_KEY",
        description="Optional shared admin API key for admin/tenant dashboards.",
    )
    dashboard_hmac_auth_enabled: bool = Field(
        default=False,
        validation_alias="DASHBOARD_HMAC_AUTH_ENABLED",
        description="Enable optional HMAC header auth path for dashboard/admin routes.",
    )
    dashboard_hmac_secret: Optional[str] = Field(
        default=None,
        validation_alias="DASHBOARD_HMAC_SECRET",
        description="Shared secret used to validate X-Admin-Signature when HMAC auth is enabled.",
    )
    dashboard_hmac_max_skew_seconds: int = Field(
        default=300,
        validation_alias="DASHBOARD_HMAC_MAX_SKEW_SECONDS",
        description="Allowed clock skew (seconds) for dashboard HMAC timestamp validation.",
    )
    angel_postback_token: Optional[str] = Field(
        default=None,
        validation_alias="ANGEL_POSTBACK_TOKEN",
        description="Shared secret token expected on direct Angel broker postbacks when ANGEL_POSTBACK_AUTH_MODE=DIRECT_BROKER.",
    )
    angel_postback_relay_token: Optional[str] = Field(
        default=None,
        validation_alias="ANGEL_POSTBACK_RELAY_TOKEN",
        description="Shared secret token expected from the authenticated relay when ANGEL_POSTBACK_AUTH_MODE=RELAY.",
    )
    auth_token_secret: Optional[str] = Field(
        default=None,
        validation_alias="AUTH_TOKEN_SECRET",
        description=(
            "Secret used to sign all /auth JWT tokens (access and refresh). "
            "Must be cryptographically random, minimum 32 bytes. "
            "Stored in OCI Vault as phoenix-auth_token_secret."
        ),
    )
    demo_auth_token_secret: Optional[str] = Field(
        default=None,
        validation_alias="DEMO_AUTH_TOKEN_SECRET",
        description="Deprecated alias for auth_token_secret. Use AUTH_TOKEN_SECRET instead.",
    )
    admin_api_key_secret: Optional[str] = Field(
        default=None,
        validation_alias="ADMIN_API_KEY_SECRET",
        description="Secret Manager secret id for admin API key.",
    )
    admin_api_key_secret_project: Optional[str] = Field(
        default=None,
        validation_alias="ADMIN_API_KEY_SECRET_PROJECT_ID",
        description="Optional override project for admin API key secret.",
    )
    admin_api_key_secret_prefix: str = Field(
        default="",
        validation_alias="ADMIN_API_KEY_SECRET_PREFIX",
        description="Optional prefix for admin API key secret ids.",
    )
    runtime_config_enabled: bool = Field(
        default=False,
        validation_alias="RUNTIME_CONFIG_ENABLED",
        description="Enable dynamic runtime config overrides from Firestore/Secret Manager.",
    )
    runtime_config_cache_seconds: float = Field(
        default=15.0,
        validation_alias="RUNTIME_CONFIG_CACHE_SECONDS",
        description="TTL for runtime override cache (seconds).",
    )
    runtime_config_firestore_collection: str = Field(
        default="runtime_config",
        validation_alias="RUNTIME_CONFIG_FIRESTORE_COLLECTION",
        description="Firestore collection containing runtime override documents.",
    )
    runtime_config_firestore_document: str = Field(
        default="global",
        validation_alias="RUNTIME_CONFIG_FIRESTORE_DOCUMENT",
        description="Firestore document id containing global runtime overrides.",
    )
    runtime_config_secret: Optional[str] = Field(
        default=None,
        validation_alias="RUNTIME_CONFIG_SECRET",
        description="Optional Secret Manager secret id containing JSON runtime overrides.",
    )
    runtime_config_secret_project: Optional[str] = Field(
        default=None,
        validation_alias="RUNTIME_CONFIG_SECRET_PROJECT_ID",
        description="Optional project override for runtime config secret.",
    )
    enable_multi_hub: bool = Field(
        default=False,
        description="Feature flag when running multiple hub instances in parallel.",
    )
    use_hub_router: bool = Field(
        default=False,
        description="Toggle to route requests/orders through the hub router layer.",
    )
    # ==== Hub defaults for single-tenant / legacy strategies ====
    hub_default_tenant_id: str = Field(
        default="tenant-1",
        validation_alias="HUB_DEFAULT_TENANT_ID",
        description=(
            "Default tenant_id used by strategy_bridge and trade_records when a caller does not "
            "provide a tenant explicitly. Must match the operator's production tenant. "
            "Default 'tenant-1' matches the bundled LIVE compose manifest."
        ),
    )
    hub_default_broker_account_id: str = Field(
        default="",
        validation_alias="HUB_DEFAULT_BROKER_ACCOUNT_ID",
        description=(
            "Legacy/single-tenant fallback broker_account_id used when callers omit a broker account. "
            "Additional tenants/broker accounts are configured in Firestore; you do not add more env vars per tenant."
        ),
    )
    hub_default_strategy_id: str = Field(
        default="legacy_strategy",
        validation_alias="HUB_DEFAULT_STRATEGY_ID",
        description=(
            "Fallback strategy_id used for legacy trade logs when no strategy is provided."
        ),
    )
    use_hub_router_for_ng_options: bool = Field(
        False, validation_alias="USE_HUB_ROUTER_FOR_NG_OPTIONS"
    )
    use_hub_router_for_nifty_options: bool = Field(
        False, validation_alias="USE_HUB_ROUTER_FOR_NIFTY_OPTIONS"
    )
    use_hub_router_for_nifty_delta: bool = Field(
        False, validation_alias="USE_HUB_ROUTER_FOR_NIFTY_DELTA"
    )
    use_hub_router_for_banknifty_options: bool = Field(
        False, validation_alias="USE_HUB_ROUTER_FOR_BANKNIFTY_OPTIONS"
    )
    use_hub_router_for_banknifty_delta: bool = Field(
        False, validation_alias="USE_HUB_ROUTER_FOR_BANKNIFTY_DELTA"
    )
    use_hub_router_for_finnifty_options: bool = Field(
        False, validation_alias="USE_HUB_ROUTER_FOR_FINNIFTY_OPTIONS"
    )
    use_hub_router_for_sensex_options: bool = Field(
        False, validation_alias="USE_HUB_ROUTER_FOR_SENSEX_OPTIONS"
    )
    use_hub_router_for_midcpnifty_options: bool = Field(
        False, validation_alias="USE_HUB_ROUTER_FOR_MIDCPNIFTY_OPTIONS"
    )
    enable_capital_checks: bool = Field(True, validation_alias="ENABLE_CAPITAL_CHECKS")
    capital_auto_resize_enabled: bool = Field(
        True,
        validation_alias="RISK_CAPITAL_AUTO_RESIZE_ENABLED",
    )
    capital_margin_short_option_per_lot: float = Field(
        default=200000.0,
        validation_alias="CAPITAL_MARGIN_SHORT_OPTION_PER_LOT",
        description="Conservative per-lot margin estimate used for short option sells when margin mode is SHADOW or ENFORCE.",
    )
    capital_margin_futures_per_lot: float = Field(
        default=200000.0,
        validation_alias="CAPITAL_MARGIN_FUTURES_PER_LOT",
        description="Conservative per-lot margin floor used for futures when margin mode is SHADOW or ENFORCE.",
    )
    capital_margin_futures_rate: float = Field(
        default=0.12,
        validation_alias="CAPITAL_MARGIN_FUTURES_RATE",
        description="Futures margin estimate as a fraction of futures notional when margin mode is SHADOW or ENFORCE.",
    )
    order_router_enforce_idempotency: bool = Field(
        default=False,
        validation_alias="ORDER_ROUTER_ENFORCE_IDEMPOTENCY",
        description="Enable idempotency claim-before-submit in OrderRouter when idempotency_key is provided.",
    )
    order_router_enforce_global_kill_switch: bool = Field(
        default=False,
        validation_alias="ORDER_ROUTER_ENFORCE_GLOBAL_KILL_SWITCH",
        description="Enforce GLOBAL_KILL maintenance switch in OrderRouter interceptor pipeline.",
    )
    position_ownership_enabled: bool = Field(
        default=False,
        validation_alias="POSITION_OWNERSHIP_ENABLED",
        description="Enable strategy ownership lock policy in OrderRouter.",
    )
    position_ownership_unknown_mode: str = Field(
        default="block_entries",
        validation_alias="POSITION_OWNERSHIP_UNKNOWN_MODE",
        description="Unknown ownership handling: block_entries|allow_entries.",
    )
    position_ownership_allow_system_exit: bool = Field(
        default=True,
        validation_alias="POSITION_OWNERSHIP_ALLOW_SYSTEM_EXIT",
        description="Allow explicit system-safety exit bypass for ownership lock.",
    )
    ownership_persist_pending_locks: bool = Field(
        default=True,
        validation_alias="OWNERSHIP_PERSIST_PENDING_LOCKS",
        description="Persist pending ownership locks using reserved ledger rows.",
    )
    auto_strategy_select_enabled: bool = Field(
        default=False,
        validation_alias="AUTO_STRATEGY_SELECT_ENABLED",
        description="Enable adaptive strategy auto-selection by market regime.",
    )
    auto_strategy_max_active_per_underlying: int = Field(
        default=2,
        validation_alias="AUTO_STRATEGY_MAX_ACTIVE_PER_UNDERLYING",
        description="Maximum active strategies per underlying when auto-selection is enabled.",
    )
    auto_strategy_min_hold_seconds: float = Field(
        default=180.0,
        validation_alias="AUTO_STRATEGY_MIN_HOLD_SECONDS",
        description="Minimum hold window before switching selected strategies.",
    )
    order_lifecycle_persist_markers_required: bool = Field(
        default=True,
        validation_alias="ORDER_LIFECYCLE_PERSIST_MARKERS_REQUIRED",
        description="Fail startup if durable order lifecycle processed marker backend is unavailable.",
    )
    capital_fail_closed_on_missing_state: bool = Field(
        True,
        validation_alias="CAPITAL_FAIL_CLOSED_ON_MISSING_STATE",
    )
    capital_fail_closed_on_missing_notional_price: bool = Field(
        True,
        validation_alias="CAPITAL_FAIL_CLOSED_ON_MISSING_NOTIONAL_PRICE",
    )
    capital_limits_json: str = Field(
        default="",
        validation_alias="CAPITAL_LIMITS_JSON",
        description="JSON object mapping tenant:account keys to per-account capital limit dicts.",
    )
    allow_live_capital_limits_default_only: bool = Field(
        default=False,
        validation_alias="ALLOW_LIVE_CAPITAL_LIMITS_DEFAULT_ONLY",
        description="If true, allow LIVE startup with empty CAPITAL_LIMITS_JSON (audited exception).",
    )
    enable_risk_checks: bool = Field(True, validation_alias="ENABLE_RISK_CHECKS")
    risk_enable_daily_loss: bool = Field(False, validation_alias="RISK_ENABLE_DAILY_LOSS")
    risk_max_daily_loss: Optional[float] = Field(
        default=None,
        validation_alias="RISK_MAX_DAILY_LOSS",
        description="Absolute daily loss limit per account.",
    )
    risk_live_min_daily_loss_inr: float = Field(
        default=5000.0,
        validation_alias="RISK_LIVE_MIN_DAILY_LOSS_INR",
        description="Suspiciously-low floor for LIVE RISK_MAX_DAILY_LOSS validation.",
    )
    risk_live_low_daily_loss_action: str = Field(
        default="warn",
        validation_alias="RISK_LIVE_LOW_DAILY_LOSS_ACTION",
        description=(
            "Action for LIVE daily-loss values below the configured floor: warn or error."
        ),
    )
    risk_fail_open_on_missing_pnl: bool = Field(
        default=False,
        validation_alias="RISK_FAIL_OPEN_ON_MISSING_PNL",
        description="If true, allow orders when shared PnL state is unavailable.",
    )
    risk_fail_closed_on_missing_pnl: bool = Field(
        default=False,
        validation_alias="RISK_FAIL_CLOSED_ON_MISSING_PNL",
        description="If true, block new entries when shared PnL state is unavailable while still allowing exits.",
    )
    risk_include_unrealized_in_daily_loss: bool = Field(
        default=True,
        validation_alias="RISK_INCLUDE_UNREALIZED_IN_DAILY_LOSS",
        description="If true, enforce daily loss protection on realized plus unrealized MTM.",
    )
    circuit_breaker_persist_state: bool = Field(
        default=True,
        validation_alias="CIRCUIT_BREAKER_PERSIST_STATE",
        description="Persist circuit breaker state across restarts using the configured control-plane store.",
    )
    # ==== Profit booking (daily target) ====
    profit_enable_daily_target: bool = Field(False, validation_alias="PROFIT_ENABLE_DAILY_TARGET")
    profit_daily_target: Optional[float] = Field(
        default=None,
        validation_alias="PROFIT_DAILY_TARGET",
        description="Absolute daily profit target in account currency.",
    )
    profit_daily_target_paper: Optional[float] = Field(
        default=None,
        validation_alias="PROFIT_DAILY_TARGET_PAPER",
        description="Optional daily profit target override when running in PAPER mode.",
    )
    # --- Profit management (daily target) ---
    enable_profit_checks: bool = Field(False, validation_alias="ENABLE_PROFIT_CHECKS")
    profit_block_new_orders_on_target: bool = Field(
        True, validation_alias="PROFIT_BLOCK_NEW_ORDERS_ON_TARGET"
    )
    profit_lock_enabled: bool = Field(
        default=True,
        validation_alias="PROFIT_LOCK_ENABLED",
        description="Enable trailing profit lock once daily target is reached.",
    )
    profit_lock_giveback_pct: float = Field(
        default=0.1,
        validation_alias="PROFIT_LOCK_GIVEBACK_PCT",
        description="Allowed giveback from peak total PnL (0.0-1.0).",
    )
    profit_lock_exit_cooldown_seconds: float = Field(
        default=30.0,
        validation_alias="PROFIT_LOCK_EXIT_COOLDOWN_SECONDS",
        description="Cooldown between profit-lock exit attempts.",
    )
    profit_lock_require_signal: bool = Field(
        default=True,
        validation_alias="PROFIT_LOCK_REQUIRE_SIGNAL",
        description="Exit if strategy signal becomes invalid after lock.",
    )
    # --- Per-position trailing profit lock ---
    # Independent of the account-level profit_lock_*. Tracks peak unrealized
    # P&L per (account, symbol) and exits a single position when its current
    # unrealized P&L falls below peak * (1 - giveback_pct), provided the peak
    # has crossed the arming floor.
    position_trailing_lock_enabled: bool = Field(
        default=False,
        validation_alias="POSITION_TRAILING_LOCK_ENABLED",
        description=(
            "Enable per-position trailing profit lock. Each open position "
            "tracks its own peak unrealized P&L; the position is exited when "
            "current P&L falls below peak * (1 - giveback_pct)."
        ),
    )
    position_trailing_lock_giveback_pct: float = Field(
        default=0.10,
        validation_alias="POSITION_TRAILING_LOCK_GIVEBACK_PCT",
        description="Allowed giveback from a position's peak unrealized P&L (0.0-1.0).",
    )
    position_trailing_lock_floor_inr: float = Field(
        default=500.0,
        validation_alias="POSITION_TRAILING_LOCK_FLOOR_INR",
        description=(
            "Minimum peak unrealized P&L (INR) before per-position trailing "
            "arms. Below this peak the lock will not fire — prevents churn on "
            "tiny P&L wiggles."
        ),
    )
    position_trailing_lock_exit_cooldown_seconds: float = Field(
        default=30.0,
        validation_alias="POSITION_TRAILING_LOCK_EXIT_COOLDOWN_SECONDS",
        description="Cooldown between per-position trailing-lock exit attempts for the same symbol.",
    )
    # Issue #225 (PR #236 review): per-position inflight idempotency
    # marker max age. Default 120s — must be larger than the watchdog
    # cadence (``hub_subscription_poll_interval`` default 60s) so the
    # marker outlives at least one full poll cycle. If this is set
    # smaller than the watchdog cadence, the marker can time out
    # BEFORE the next evaluate cycle fires, allowing a duplicate
    # submission against the same in-flight broker order — recreating
    # the 2026-05-08 duplicate-fill scenario.
    position_trailing_lock_inflight_max_seconds: float = Field(
        default=120.0,
        validation_alias="POSITION_TRAILING_LOCK_INFLIGHT_MAX_SECONDS",
        description=(
            "Max age of an inflight trailing-lock marker before it is "
            "auto-cleared with an ERROR event (issue #225). MUST be "
            "larger than the engine's watchdog cadence."
        ),
    )
    enable_eod_exit: bool = Field(
        default=False,
        validation_alias="ENABLE_EOD_EXIT",
        description="Toggle to force exit all open positions at end-of-day.",
    )
    eod_exit_time: str = Field(
        default="15:20",
        validation_alias="EOD_EXIT_TIME",
        description="Local EOD square-off time (HH:MM) in default_time_zone.",
    )
    eod_exit_excluded_exchanges: str = Field(
        default="",
        validation_alias="EOD_EXIT_EXCLUDED_EXCHANGES",
        description="Comma-separated exchange segments to exclude from EOD exits (e.g., MCX).",
    )
    eod_exit_retry_on_no_eligible: bool = Field(
        default=False,
        validation_alias="EOD_EXIT_RETRY_ON_NO_ELIGIBLE",
        description="Retry EOD exits when no eligible positions are found instead of marking complete immediately.",
    )
    eod_exit_require_fresh_position_sync: bool = Field(
        default=False,
        validation_alias="EOD_EXIT_REQUIRE_FRESH_POSITION_SYNC",
        description="Require fresh position sync status (OK + recent last_ok_ts) before concluding no positions.",
    )
    eod_exit_positions_max_age_seconds: int = Field(
        default=60,
        validation_alias="EOD_EXIT_POSITIONS_MAX_AGE_SECONDS",
        description="Maximum allowed age (seconds) for positions_last_ok_ts when fresh position gating is enabled.",
    )
    eod_exit_position_telemetry: bool = Field(
        default=True,
        validation_alias="EOD_EXIT_POSITION_TELEMETRY",
        description="Emit EOD position telemetry (fetched/non-zero/eligible/excluded counts) on each EOD evaluation cycle.",
    )
    eod_exit_retry_cutoff_time: str = Field(
        default="15:30",
        validation_alias="EOD_EXIT_RETRY_CUTOFF_TIME",
        description="Retry cutoff time (HH:MM) for EOD exits when retry-on-no-eligible is enabled.",
    )
    eod_cancel_open_orders_enabled: bool = Field(
        default=True,
        validation_alias="EOD_CANCEL_OPEN_ORDERS_ENABLED",
        description="Cancel outstanding open intraday orders during the EOD exit cycle.",
    )
    eod_cancel_retry_loop_enabled: bool = Field(
        default=False,
        validation_alias="EOD_CANCEL_RETRY_LOOP_ENABLED",
        description="Enable a bounded immediate retry loop for transient EOD open-order cancel failures before falling back to the broader retry window.",
    )
    position_sync_stale_close_guard: bool = Field(
        default=False,
        validation_alias="POSITION_SYNC_STALE_CLOSE_GUARD",
        description="Guard stale-close actions when broker position snapshots look suspicious or truncated.",
    )

    # ==== Dual Profit Sweep Strategy (NEW) ====
    profit_sweep_enabled: bool = Field(
        default=True,
        validation_alias="PROFIT_SWEEP_ENABLED",
        description="Master toggle for profit sweep feature (SIMPLE or MULTIPLE modes).",
    )
    profit_sweep_strategy: str = Field(
        default="SIMPLE",
        validation_alias="PROFIT_SWEEP_STRATEGY",
        description="Sweep strategy: 'SIMPLE' (once per day) or 'MULTIPLE' (multiple per day with cooldown).",
    )
    profit_simple_enabled: bool = Field(
        default=True,
        validation_alias="PROFIT_SIMPLE_ENABLED",
        description="Enable SIMPLE strategy (single daily sweep at lower target).",
    )
    profit_multiple_enabled: bool = Field(
        default=False,
        validation_alias="PROFIT_MULTIPLE_ENABLED",
        description="Enable MULTIPLE strategy (multiple sweeps per day with cooldown).",
    )
    profit_multiple_target: Optional[float] = Field(
        default=None,
        validation_alias="PROFIT_MULTIPLE_TARGET",
        description="Profit target for each sweep in MULTIPLE strategy (₹).",
    )
    profit_sweep_cooldown_minutes: float = Field(
        default=30.0,
        validation_alias="PROFIT_SWEEP_COOLDOWN_MINUTES",
        description="Minutes to wait between sweeps in MULTIPLE strategy.",
    )
    profit_max_sweeps_per_day: int = Field(
        default=5,
        validation_alias="PROFIT_MAX_SWEEPS_PER_DAY",
        description="Maximum sweeps per day in MULTIPLE strategy.",
    )
    profit_simple_target_mode: str = Field(
        default="absolute",
        validation_alias="PROFIT_SIMPLE_TARGET_MODE",
        description="Target mode for SIMPLE sweep: 'absolute' or 'premium_pct'.",
    )
    profit_simple_target_premium_pct: float = Field(
        default=0.5,
        validation_alias="PROFIT_SIMPLE_TARGET_PREMIUM_PCT",
        description="When mode=premium_pct, exit target fraction of collected open premium.",
    )

    bigquery_trades_table: str = Field(
        default="trades",
        description="Table name or fully-qualified id for trade events.",
    )
    indicator_bq_table: str = Field(
        default="indicator_bars",
        description="Table name or fully-qualified id for indicator bars (if enabled).",
    )
    bigquery_pnl_table: str = Field(
        default="pnl",
        description="Table name or fully-qualified id for PnL snapshots.",
    )
    bq_orders_table: str = Field(
        default="orders",
        description="BigQuery table for hub order records (multi-tenant).",
    )
    bq_trades_table: str = Field(
        default="trades",
        description="BigQuery table for hub trade records (multi-tenant).",
    )
    bq_daily_pnl_table: str = Field(
        default="daily_pnl",
        description="BigQuery table for daily PnL snapshots (multi-tenant).",
    )

    @model_validator(mode="after")
    def validate_position_trailing_lock_inflight_window(self) -> Settings:
        """Issue #225 (PR #236 review P2): validate the inflight window.

        Round-3 review P2: skip validation when trailing-lock is
        DISABLED — there is no inflight protection to validate, and
        operators legitimately may not have configured the inflight
        window when ``POSITION_TRAILING_LOCK_ENABLED=false``.

        Round-4 review P2/P3:
          - Reject non-finite values (NaN, +/-inf). NaN comparisons
            are always False so an unchecked NaN max age makes every
            marker time out immediately, silently disabling the
            duplicate-fill guard; +inf leaves stale markers blocking
            exits forever.
          - Compare against the EFFECTIVE watchdog interval. The
            profit watchdog clamps ``hub_subscription_poll_interval``
            with ``max(10.0, interval)`` in ``app/hub/hub.py``; an
            inflight window of 5s combined with a configured poll of
            1s passes the raw check but expires before the actual
            10s evaluate cycle.
        """
        if not bool(getattr(self, "position_trailing_lock_enabled", False)):
            return self
        import math
        inflight_max = float(
            getattr(self, "position_trailing_lock_inflight_max_seconds", 0.0) or 0.0
        )
        if not math.isfinite(inflight_max):
            raise ValueError(
                "POSITION_TRAILING_LOCK_INFLIGHT_MAX_SECONDS must be a "
                f"finite number; got {inflight_max!r}. NaN expires every "
                "marker immediately (silently disabling protection); "
                "+/-inf leaves stale markers blocking exits forever."
            )
        if inflight_max <= 0.0:
            raise ValueError(
                "POSITION_TRAILING_LOCK_INFLIGHT_MAX_SECONDS must be > 0 "
                "when POSITION_TRAILING_LOCK_ENABLED=true; "
                f"got {inflight_max}. Setting it to 0 silently disables "
                "the trailing-lock duplicate-fill protection (issue #225)."
            )
        # Effective watchdog floor — the profit watchdog clamps the
        # configured poll interval at 10s, so the inflight window must
        # outlast the LARGER of the configured interval and the 10s
        # floor.
        raw_poll_interval = float(
            getattr(self, "hub_subscription_poll_interval", 0.0) or 0.0
        )
        effective_poll_interval = max(10.0, raw_poll_interval) if raw_poll_interval > 0.0 else 10.0
        if inflight_max <= effective_poll_interval:
            raise ValueError(
                "POSITION_TRAILING_LOCK_INFLIGHT_MAX_SECONDS "
                f"({inflight_max}) must be larger than the effective "
                f"watchdog interval ({effective_poll_interval}s — the "
                f"larger of HUB_SUBSCRIPTION_POLL_INTERVAL "
                f"({raw_poll_interval}) and the 10s clamp floor in "
                "hub.py). Otherwise the inflight marker can expire "
                "before the next watchdog evaluation, recreating the "
                "2026-05-08 duplicate-fill scenario (issue #225). Set "
                f"the inflight window to at least "
                f"{effective_poll_interval + 30} seconds."
            )
        return self

    @model_validator(mode="after")
    def validate_profit_sweep_settings(self) -> Settings:
        """Validate profit sweep configuration."""
        configured_profit_sweep_fields = {
            "profit_sweep_enabled",
            "profit_sweep_strategy",
            "profit_simple_enabled",
            "profit_multiple_enabled",
            "profit_daily_target",
            "profit_daily_target_paper",
            "profit_multiple_target",
            "profit_sweep_cooldown_minutes",
            "profit_max_sweeps_per_day",
            "profit_simple_target_mode",
            "profit_simple_target_premium_pct",
        }
        profit_sweep_explicit = any(
            field in self.model_fields_set for field in configured_profit_sweep_fields
        )
        # Validate strategy value
        if self.profit_sweep_strategy not in ["SIMPLE", "MULTIPLE"]:
            raise ValueError(
                f"profit_sweep_strategy must be 'SIMPLE' or 'MULTIPLE', "
                f"got '{self.profit_sweep_strategy}'"
            )

        # Validate profit_simple_target_mode
        _target_mode = str(getattr(self, "profit_simple_target_mode", "absolute") or "absolute").lower()
        if _target_mode not in {"absolute", "premium_pct"}:
            raise ValueError(
                f"profit_simple_target_mode must be 'absolute' or 'premium_pct', "
                f"got '{_target_mode}'"
            )

        validate_simple_strategy = (
            profit_sweep_explicit and self.profit_sweep_strategy == "SIMPLE"
        )
        validate_multiple_strategy = (
            profit_sweep_explicit and self.profit_sweep_strategy == "MULTIPLE"
        )

        # Validate SIMPLE strategy only when sweep controls were explicitly configured.
        if validate_simple_strategy:
            _tmode = str(getattr(self, "profit_simple_target_mode", "absolute") or "absolute").lower()
            if _tmode != "premium_pct":
                if self.profit_daily_target is None or self.profit_daily_target <= 0:
                    raise ValueError(
                        f"SIMPLE strategy requires profit_daily_target > 0 when mode=absolute, "
                        f"got {self.profit_daily_target}"
                    )

        # Validate MULTIPLE strategy only when sweep controls were explicitly configured.
        if validate_multiple_strategy:
            if self.profit_multiple_target is None or self.profit_multiple_target <= 0:
                raise ValueError(
                    f"MULTIPLE strategy requires profit_multiple_target > 0, "
                    f"got {self.profit_multiple_target}"
                )
            if self.profit_sweep_cooldown_minutes < 5:
                raise ValueError(
                    f"profit_sweep_cooldown_minutes must be >= 5, "
                    f"got {self.profit_sweep_cooldown_minutes}"
                )
            if self.profit_max_sweeps_per_day <= 0:
                raise ValueError(
                    f"profit_max_sweeps_per_day must be > 0, "
                    f"got {self.profit_max_sweeps_per_day}"
                )

        if self.profit_lock_giveback_pct < 0 or self.profit_lock_giveback_pct >= 1:
            raise ValueError(
                "profit_lock_giveback_pct must be >= 0 and < 1"
            )
        if self.profit_lock_exit_cooldown_seconds < 0:
            raise ValueError(
                "profit_lock_exit_cooldown_seconds must be >= 0"
            )
        ownership_unknown_mode = str(
            self.position_ownership_unknown_mode or ""
        ).strip().lower()
        if ownership_unknown_mode not in {"block_entries", "allow_entries"}:
            raise ValueError(
                "position_ownership_unknown_mode must be one of: block_entries, allow_entries"
            )
        self.position_ownership_unknown_mode = ownership_unknown_mode
        if int(self.auto_strategy_max_active_per_underlying or 0) < 1:
            raise ValueError(
                "auto_strategy_max_active_per_underlying must be >= 1"
            )
        if float(self.auto_strategy_min_hold_seconds or 0.0) < 0.0:
            raise ValueError("auto_strategy_min_hold_seconds must be >= 0")
        schema_mode = str(self.schema_check_mode or "").strip().lower()
        if schema_mode not in {"warn", "strict"}:
            raise ValueError("schema_check_mode must be one of: warn, strict")
        self.schema_check_mode = schema_mode
        
        return self
    firestore_collection_prefix: str = Field(
        default="",
        description="Optional prefix for Firestore collections to isolate environments.",
    )
    control_plane_backend: str = Field(
        default="postgres",
        validation_alias="CONTROL_PLANE_BACKEND",
        description="Control plane store backend: firestore|postgres.",
    )
    sweep_state_backend: str = Field(
        default="postgres",
        validation_alias="SWEEP_STATE_BACKEND",
        description="Sweep state store backend: firestore|postgres.",
    )
    control_plane_db_dsn: Optional[str] = Field(
        default=None,
        validation_alias="CONTROL_PLANE_DB_DSN",
        description="Postgres DSN for control plane (postgresql://user:pass@host:port/db).",
    )
    sweep_state_db_dsn: Optional[str] = Field(
        default=None,
        validation_alias="SWEEP_STATE_DB_DSN",
        description="Postgres DSN for sweep state (defaults to CONTROL_PLANE_DB_DSN).",
    )
    control_plane_pg_host: Optional[str] = Field(
        default=None,
        validation_alias="CONTROL_PLANE_PG_HOST",
        description="Postgres host for control plane DB.",
    )
    control_plane_pg_port: Optional[int] = Field(
        default=None,
        validation_alias="CONTROL_PLANE_PG_PORT",
        description="Postgres port for control plane DB.",
    )
    control_plane_pg_db: Optional[str] = Field(
        default=None,
        validation_alias="CONTROL_PLANE_PG_DB",
        description="Postgres database name for control plane.",
    )
    control_plane_pg_user: Optional[str] = Field(
        default=None,
        validation_alias="CONTROL_PLANE_PG_USER",
        description="Postgres user for control plane DB.",
    )
    control_plane_pg_password: Optional[str] = Field(
        default=None,
        validation_alias="CONTROL_PLANE_PG_PASSWORD",
        description="Postgres password for control plane DB.",
    )
    control_plane_pg_sslmode: Optional[str] = Field(
        default=None,
        validation_alias="CONTROL_PLANE_PG_SSLMODE",
        description="Postgres sslmode for control plane DB.",
    )
    control_plane_dual_read_compare: bool = Field(
        default=False,
        validation_alias="CONTROL_PLANE_DUAL_READ_COMPARE",
        description="When true and backend=postgres, also read Firestore and log diffs.",
    )
    control_plane_dual_write: bool = Field(
        default=False,
        validation_alias="CONTROL_PLANE_DUAL_WRITE",
        description="When true and backend=postgres, also write to Firestore (best-effort).",
    )
    control_plane_dual_read_log_limit: int = Field(
        default=5,
        validation_alias="CONTROL_PLANE_DUAL_READ_LOG_LIMIT",
        description="Max number of diff records logged per call.",
    )
    secret_manager_prefix: str = Field(
        default="",
        description="Optional Secret Manager prefix for broker credentials.",
    )
    broker_secret_backend: str = Field(
        default="env",
        validation_alias="BROKER_SECRET_BACKEND",
        description=(
            "Broker credentials backend: postgres|secret_manager|env|auto. "
            "LIVE requires secret_manager or postgres."
        ),
    )
    broker_secret_project_id: str = Field(
        default="",
        description="Optional override project for broker credential secrets (defaults to gcp_project_id).",
    )
    broker_secret_prefix: str = Field(
        default="",
        description="Optional prefix for broker credential secret ids.",
    )
    hub_instance_name: str = Field(
        default="",
        description="Identifier for the hub instance when multiple hubs run per project.",
    )
    hub_subscription_poll_interval: float = Field(
        default=60.0,
        description="Polling interval (seconds) for hub subscription reconciliation.",
    )
    alert_evaluation_interval_seconds: float = Field(
        default=30.0,
        validation_alias="ALERT_EVALUATION_INTERVAL_SECONDS",
        description="Polling interval (seconds) for background alert-rule evaluation.",
    )
    hub_reconcile_verbose: bool = Field(
        default=False,
        validation_alias="HUB_RECONCILE_VERBOSE",
        description="Enable verbose per-account/per-subscription reconcile INFO logging.",
    )
    schema_check_mode: str = Field(
        default="warn",
        validation_alias="SCHEMA_CHECK_MODE",
        description="Startup schema compatibility check mode: warn|strict.",
    )
    app_runtime_startup_validate: bool = Field(
        default=False,
        validation_alias="APP_RUNTIME_STARTUP_VALIDATE",
        description="Enable strict startup configuration validation prior to runtime startup.",
    )
    # ==== Startup audit flags ====
    allow_live_capital_checks_disabled: bool = Field(
        default=False,
        validation_alias="ALLOW_LIVE_CAPITAL_CHECKS_DISABLED",
        description=(
            "STARTUP AUDIT ONLY — does NOT disable capital checks at runtime. "
            "The actual runtime gate is ENABLE_CAPITAL_CHECKS. "
            "Setting this to true is recorded in the startup safety summary and "
            "permits startup to proceed when capital checks are explicitly disabled. "
            "Should always be false in production LIVE deployments."
        ),
    )
    # ==== Typed env vars previously read via raw os.getenv() ====
    capital_margin_check_mode: str = Field(
        default="enforce",
        validation_alias="CAPITAL_MARGIN_CHECK_MODE",
        description=(
            "Capital margin enforcement mode consumed by feature_flags.py. "
            "Choices: enforce | warn | off. "
            "LIVE mode policy gates override 'off' to 'enforce'."
        ),
    )
    position_sync_interval_seconds: int = Field(
        default=90,
        validation_alias="POSITION_SYNC_INTERVAL_SECONDS",
        description="Interval (seconds) between broker position sync polls in the stream runner.",
    )

    backend_url: str = Field(
        default="http://localhost:8080",
        description="URL of the Phoenix backend, used by the BFF proxy.",
    )

    @property
    def database_settings(self) -> DatabaseSettings:
        return DatabaseSettings(
            gcp_project_id=self.gcp_project_id,
            bq_dataset_id=self.bq_dataset_id,
            bq_orders_table=self.bq_orders_table,
            bq_trades_table=self.bq_trades_table,
            bq_daily_pnl_table=self.bq_daily_pnl_table,
            bigquery_trades_table=self.bigquery_trades_table,
            bigquery_pnl_table=self.bigquery_pnl_table,
        )

    @property
    def infrastructure_settings(self) -> InfrastructureSettings:
        return InfrastructureSettings(
            default_time_zone=self.default_time_zone,
            firestore_collection_prefix=self.firestore_collection_prefix,
            control_plane_backend=self.control_plane_backend,
            sweep_state_backend=self.sweep_state_backend,
            hub_instance_name=self.hub_instance_name,
            hub_subscription_poll_interval=self.hub_subscription_poll_interval,
        )

    @property
    def risk_guard_settings(self) -> RiskGuardSettings:
        return RiskGuardSettings(
            enable_capital_checks=self.enable_capital_checks,
            capital_auto_resize_enabled=self.capital_auto_resize_enabled,
            enable_risk_checks=self.enable_risk_checks,
            risk_enable_daily_loss=self.risk_enable_daily_loss,
            risk_max_daily_loss=self.risk_max_daily_loss,
            risk_fail_open_on_missing_pnl=self.risk_fail_open_on_missing_pnl,
            risk_include_unrealized_in_daily_loss=self.risk_include_unrealized_in_daily_loss,
            enable_profit_checks=self.enable_profit_checks,
        )

    @property
    def strategy_feature_flags(self) -> StrategyFeatureFlags:
        return StrategyFeatureFlags(
            use_hub_router=self.use_hub_router,
            use_hub_router_for_ng_options=self.use_hub_router_for_ng_options,
            use_hub_router_for_nifty_options=self.use_hub_router_for_nifty_options,
            use_hub_router_for_nifty_delta=self.use_hub_router_for_nifty_delta,
            use_hub_router_for_banknifty_options=self.use_hub_router_for_banknifty_options,
            use_hub_router_for_banknifty_delta=self.use_hub_router_for_banknifty_delta,
            use_hub_router_for_finnifty_options=self.use_hub_router_for_finnifty_options,
            use_hub_router_for_sensex_options=self.use_hub_router_for_sensex_options,
            use_hub_router_for_midcpnifty_options=self.use_hub_router_for_midcpnifty_options,
        )


@lru_cache()
def get_settings() -> Settings:
    """
    Return cached Settings for the Cloud Run multi-tenant hub.

    All services should call this instead of reading os.environ directly so
    configuration stays centralized across the control plane (Firestore),
    data plane (BigQuery), and hub-level feature flags.
    """
    settings = Settings()
    settings.admin_api_key = _resolve_admin_api_key(settings)
    return settings


@lru_cache(maxsize=1)
def _get_secret_manager_client() -> "secretmanager.SecretManagerServiceClient | None":
    if secretmanager is None:
        return None
    return secretmanager.SecretManagerServiceClient()


def _build_admin_secret_name(settings: Settings, secret_id: str) -> str:
    if secret_id.startswith("projects/"):
        return secret_id
    project_id = settings.admin_api_key_secret_project or settings.gcp_project_id
    if not project_id:
        raise RuntimeError("No project id configured for admin api key secret")
    if settings.admin_api_key_secret_prefix:
        secret_id = f"{settings.admin_api_key_secret_prefix}{secret_id}"
    return f"projects/{project_id}/secrets/{secret_id}/versions/latest"


def _resolve_admin_api_key(settings: Settings) -> Optional[str]:
    if settings.admin_api_key:
        return settings.admin_api_key
    secret_id = settings.admin_api_key_secret
    if not secret_id:
        return settings.admin_api_key
    client = _get_secret_manager_client()
    if client is None:
        logger.warning("Admin API key secret configured but Secret Manager client unavailable")
        return settings.admin_api_key
    try:
        name = _build_admin_secret_name(settings, secret_id)
        response = client.access_secret_version(name=name)
        value = response.payload.data.decode("utf-8").strip()
        return value or settings.admin_api_key
    except Exception:
        logger.exception("Failed to load admin API key from Secret Manager")
        return settings.admin_api_key
