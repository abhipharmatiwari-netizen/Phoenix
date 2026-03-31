"""Operational safeguards and EOD maintenance (A4/A5 support utilities for the hub)."""

from __future__ import annotations

import json
import logging
import os
import time as time_module
from datetime import datetime, time, timezone, timedelta, date
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_MARKET_HOLIDAY_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "market_holidays.json"
)
_SESSION_GROUP_ALIASES = {
    "BFO": "BSE",
    "BSE": "BSE",
    "BSEFO": "BSE",
    "BSE_CM": "BSE",
    "INDICES": "NSE",
    "MCX": "MCX",
    "MCX_FO": "MCX",
    "NCO": "MCX",
    "NFO": "NSE",
    "NSE": "NSE",
    "NSEFO": "NSE",
    "NSE_CM": "NSE",
}


class TradingSessionCalendar:
    _SESSION_WINDOWS = {
        "NSE": {
            "warmup_start": time(hour=9, minute=0),
            "open": time(hour=9, minute=15),
            "close": time(hour=15, minute=30),
        },
        "BSE": {
            "warmup_start": time(hour=9, minute=0),
            "open": time(hour=9, minute=15),
            "close": time(hour=15, minute=30),
        },
        "MCX": {
            "warmup_start": time(hour=8, minute=45),
            "open": time(hour=9, minute=0),
            "close": time(hour=23, minute=30),
        },
    }

    def __init__(
        self,
        *,
        holiday_config_path: Optional[str | Path] = None,
        reload_check_interval_seconds: float = 30.0,
    ) -> None:
        self.holiday_config_path = Path(
            holiday_config_path or DEFAULT_MARKET_HOLIDAY_PATH
        )
        self.reload_check_interval_seconds = max(
            1.0,
            float(reload_check_interval_seconds),
        )
        self._holiday_dates_by_group: Dict[str, set[date]] = {}
        self._loaded_mtime_ns: Optional[int] = None
        self._next_reload_check_monotonic = 0.0
        self.reload_holidays(force=True)

    @staticmethod
    def _normalize_session_group(meta: Optional[Dict[str, Any]]) -> str:
        if not isinstance(meta, dict):
            return "NSE"
        exchange = str(
            meta.get("exchange")
            or meta.get("segment")
            or meta.get("exch_seg")
            or ""
        ).strip().upper()
        exchange_type = str(meta.get("exchangeType") or "").strip()
        if exchange_type == "5":
            return "MCX"
        if exchange:
            return _SESSION_GROUP_ALIASES.get(exchange, exchange)
        return "NSE"

    @staticmethod
    def _parse_holiday_dates(values: Any) -> set[date]:
        parsed: set[date] = set()
        if not isinstance(values, list):
            return parsed
        for raw in values:
            text = str(raw or "").strip()
            if not text:
                continue
            try:
                parsed.add(date.fromisoformat(text))
            except ValueError:
                logger.warning("Ignoring invalid market holiday value: %s", text)
        return parsed

    def reload_holidays(self, *, force: bool = False) -> None:
        now_mono = time_module.monotonic()
        if not force and now_mono < self._next_reload_check_monotonic:
            return
        self._next_reload_check_monotonic = (
            now_mono + self.reload_check_interval_seconds
        )
        if not self.holiday_config_path.exists():
            self._holiday_dates_by_group = {}
            self._loaded_mtime_ns = None
            return
        try:
            stat_result = self.holiday_config_path.stat()
            mtime_ns = int(stat_result.st_mtime_ns)
        except OSError as exc:
            logger.warning(
                "Failed to stat market holiday config %s: %s",
                self.holiday_config_path,
                exc,
            )
            return
        if not force and self._loaded_mtime_ns == mtime_ns:
            return
        try:
            payload = json.loads(self.holiday_config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "Failed to load market holiday config %s: %s",
                self.holiday_config_path,
                exc,
            )
            return
        holidays_block = payload.get("holidays", payload) if isinstance(payload, dict) else {}
        parsed: Dict[str, set[date]] = {}
        if isinstance(holidays_block, dict):
            for group, values in holidays_block.items():
                normalized_group = _SESSION_GROUP_ALIASES.get(
                    str(group or "").strip().upper(),
                    str(group or "").strip().upper(),
                )
                if not normalized_group:
                    continue
                parsed[normalized_group] = self._parse_holiday_dates(values)
        self._holiday_dates_by_group = parsed
        self._loaded_mtime_ns = mtime_ns

    def session_phase(self, meta: Optional[Dict[str, Any]], ts: datetime) -> str:
        self.reload_holidays()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        group = self._normalize_session_group(meta)
        ts_ist = ts.astimezone(IST)
        if ts_ist.date() in self._holiday_dates_by_group.get(group, set()):
            return "holiday"
        window = self._SESSION_WINDOWS.get(group, self._SESSION_WINDOWS["NSE"])
        current_time = ts_ist.time()
        if window["warmup_start"] <= current_time < window["open"]:
            return "warmup"
        if window["open"] <= current_time <= window["close"]:
            return "open"
        return "closed"

    def is_trading_open(self, meta: Optional[Dict[str, Any]], ts: datetime) -> bool:
        return self.session_phase(meta, ts) == "open"

    def is_session_active(
        self,
        meta: Optional[Dict[str, Any]],
        ts: datetime,
        *,
        include_warmup: bool = False,
    ) -> bool:
        phase = self.session_phase(meta, ts)
        return phase == "open" or (include_warmup and phase == "warmup")

    def is_holiday(self, meta: Optional[Dict[str, Any]], ts: datetime) -> bool:
        return self.session_phase(meta, ts) == "holiday"


_DEFAULT_TRADING_SESSION_CALENDAR: Optional[TradingSessionCalendar] = None


def get_trading_session_calendar() -> TradingSessionCalendar:
    global _DEFAULT_TRADING_SESSION_CALENDAR
    config_path_raw = str(
        os.getenv("MARKET_HOLIDAY_CONFIG_PATH", DEFAULT_MARKET_HOLIDAY_PATH)
    ).strip()
    config_path = Path(config_path_raw or DEFAULT_MARKET_HOLIDAY_PATH)
    reload_interval = os.getenv("MARKET_HOLIDAY_RELOAD_SECONDS", "30")
    try:
        reload_check_interval_seconds = max(1.0, float(reload_interval))
    except (TypeError, ValueError):
        reload_check_interval_seconds = 30.0
    if (
        _DEFAULT_TRADING_SESSION_CALENDAR is None
        or _DEFAULT_TRADING_SESSION_CALENDAR.holiday_config_path != config_path
        or _DEFAULT_TRADING_SESSION_CALENDAR.reload_check_interval_seconds
        != reload_check_interval_seconds
    ):
        _DEFAULT_TRADING_SESSION_CALENDAR = TradingSessionCalendar(
            holiday_config_path=config_path,
            reload_check_interval_seconds=reload_check_interval_seconds,
        )
    else:
        _DEFAULT_TRADING_SESSION_CALENDAR.reload_holidays()
    return _DEFAULT_TRADING_SESSION_CALENDAR


class MaintenanceManager:
    # Initialize heartbeat and end-of-day safeguards.
    def __init__(
        self,
        *,
        kill_switch_env: str = "GLOBAL_KILL",
        heartbeat_timeout_seconds: int = 45,
        feed_stale_log_throttle_seconds: int = 60,
        eod_square_off_time: str = "15:20",  # HH:MM (24h) - kept for backward compatibility (used as end time)
        eod_square_off_start_time: str = "15:15",
        eod_square_off_end_time: Optional[str] = None,
    ) -> None:
        self.kill_switch_env = kill_switch_env
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.feed_stale_log_throttle_seconds = max(
            0.0, float(feed_stale_log_throttle_seconds)
        )
        self.eod_start_time = self._parse_time(eod_square_off_start_time)
        # Prefer explicit end time, fallback to legacy single time
        self.eod_end_time = self._parse_time(
            eod_square_off_end_time or eod_square_off_time
        )
        self.last_tick_by_label: Dict[str, datetime] = {}
        self._square_off_done = False
        self._current_eod_date: Optional[date] = None
        self._eod_processed_underlyings: Dict[date, set[str]] = {}
        self._eod_warning_logged_date: Optional[date] = None
        self._stale_warning_state: Dict[str, Dict[str, Any]] = {}
        self._entry_block_reasons: Dict[str, set[str]] = {}

    # Parse an HH:MM string into a time object.
    @staticmethod
    def _parse_time(value: str) -> Optional[time]:
        if not value:
            return None
        try:
            hour, minute = (int(part) for part in value.split(":"))
            return time(hour=hour, minute=minute)
        except Exception as exc:  # pragma: no cover
            logger.warning("Invalid EOD time %s: %s", value, exc)
            return None

    # Check the global kill switch env flag.
    def is_kill_switch_enabled(self) -> bool:
        val = os.getenv(self.kill_switch_env, "0").strip().lower()
        return val in {"1", "true", "yes"}

    # Update last tick timestamps and reset EOD flags when needed.
    def update_tick(self, label: str, timestamp: datetime) -> None:
        self.last_tick_by_label[label] = timestamp
        if label in self._stale_warning_state:
            self._stale_warning_state[label]["last_warn_ts"] = None
            self._stale_warning_state[label]["suppressed"] = 0
        if not self.eod_end_time:
            return
        ts_ist = timestamp.astimezone(IST)
        self._reset_eod_state_if_new_day(ts_ist)
        if (
            self.eod_start_time
            and ts_ist.time() < self.eod_start_time
            and self._square_off_done
        ):
            self._square_off_done = False
            self._eod_warning_logged_date = None

    # Determine if new entries are allowed based on kill switch and heartbeat.
    def allow_new_entries(self, label: Optional[str] = None) -> bool:
        if self.is_kill_switch_enabled():
            logger.warning(
                "Kill switch enabled (%s=%s), blocking new entries",
                self.kill_switch_env,
                os.getenv(self.kill_switch_env),
            )
            return False
        if label:
            block_reasons = self.entry_block_reasons(label)
            if block_reasons:
                logger.warning(
                    "[%s] Entry blocks active: %s",
                    label,
                    ", ".join(sorted(block_reasons)),
                )
                return False
            stale_delta = self._stale_for_label(label)
            if stale_delta is not None and stale_delta > self.heartbeat_timeout_seconds:
                logger.warning(
                    "[%s] Heartbeat stale for %ds (timeout=%ds), blocking new entries",
                    label,
                    int(stale_delta),
                    self.heartbeat_timeout_seconds,
                )
                return False
            return True
        else:
            blocked_labels = {
                blocked_label: sorted(reasons)
                for blocked_label, reasons in self._entry_block_reasons.items()
                if reasons
            }
            if blocked_labels:
                summary = ", ".join(
                    f"{blocked_label}({','.join(reasons)})"
                    for blocked_label, reasons in sorted(blocked_labels.items())
                )
                logger.warning("Explicit entry blocks active: %s", summary)
                return False
            stale = self._stale_labels()
            if stale:
                logger.warning("Heartbeat stale for labels: %s", ", ".join(stale))
                return False
        return True

    # Block new entries for a label until the reason is cleared.
    def block_new_entries(self, label: str, *, reason: str) -> None:
        normalized_label = str(label or "").strip().upper()
        normalized_reason = str(reason or "").strip() or "manual_block"
        if not normalized_label:
            return
        self._entry_block_reasons.setdefault(normalized_label, set()).add(
            normalized_reason
        )

    # Clear one or all explicit entry-block reasons for a label.
    def clear_entry_block(self, label: str, *, reason: Optional[str] = None) -> None:
        normalized_label = str(label or "").strip().upper()
        if not normalized_label:
            return
        if reason is None:
            self._entry_block_reasons.pop(normalized_label, None)
            return
        reasons = self._entry_block_reasons.get(normalized_label)
        if not reasons:
            return
        reasons.discard(str(reason))
        if not reasons:
            self._entry_block_reasons.pop(normalized_label, None)

    # Return active explicit entry-block reasons for a label.
    def entry_block_reasons(self, label: str) -> set[str]:
        normalized_label = str(label or "").strip().upper()
        if not normalized_label:
            return set()
        return set(self._entry_block_reasons.get(normalized_label, set()))

    # Return seconds since last tick for a label, if available.
    def _stale_for_label(self, label: str) -> Optional[float]:
        if self.heartbeat_timeout_seconds <= 0:
            return None
        ts = self.last_tick_by_label.get(label)
        if not ts:
            return None
        now = datetime.now(timezone.utc)
        return (now - ts).total_seconds()

    # Return all labels whose ticks are stale beyond the timeout.
    def _stale_labels(
        self,
        label_filter: Optional[Callable[[str], bool]] = None,
    ) -> Dict[str, float]:
        if self.heartbeat_timeout_seconds <= 0:
            return {}
        now = datetime.now(timezone.utc)
        stale = {}
        for label, ts in self.last_tick_by_label.items():
            if label_filter is not None and not label_filter(label):
                continue
            delta = (now - ts).total_seconds()
            if delta > self.heartbeat_timeout_seconds:
                stale[label] = delta
        return stale

    # Log warnings for any stale tick streams with throttling.
    def check_heartbeat(
        self,
        *,
        label_filter: Optional[Callable[[str], bool]] = None,
        suppress_labels: Optional[set[str]] = None,
    ) -> None:
        stale = self._stale_labels(label_filter=label_filter)
        if not stale:
            return
        suppressed = suppress_labels or set()
        now = datetime.now(timezone.utc)
        for label, delta in stale.items():
            if label in suppressed:
                continue
            state = self._stale_warning_state.setdefault(
                label, {"last_warn_ts": None, "suppressed": 0}
            )
            last_warn = state.get("last_warn_ts")
            if (
                last_warn is not None
                and (now - last_warn).total_seconds()
                < self.feed_stale_log_throttle_seconds
            ):
                state["suppressed"] = int(state.get("suppressed", 0)) + 1
                continue
            suppressed = int(state.get("suppressed", 0))
            state["suppressed"] = 0
            state["last_warn_ts"] = now
            suffix = (
                f" (suppressed {suppressed} repeats)" if suppressed else ""
            )
            logger.warning(
                "Heartbeat stale for %s: %ds (timeout=%ds)%s",
                label,
                int(delta),
                self.heartbeat_timeout_seconds,
                suffix,
            )

    # Check whether the current time is within the EOD square-off window.
    def should_square_off(self, now: datetime) -> bool:
        if not self.eod_start_time or not self.eod_end_time:
            return False
        now_ist = now.astimezone(IST)
        self._reset_eod_state_if_new_day(now_ist)
        return self.eod_start_time <= now_ist.time() <= self.eod_end_time

    # Mark that square-off actions have been performed.
    def mark_square_off_done(self) -> None:
        self._square_off_done = True

    # Track which underlyings were processed during EOD square-off.
    def mark_eod_processed(self, underlyings: set[str], now: datetime) -> None:
        if not underlyings:
            return
        now_ist = now.astimezone(IST)
        self._reset_eod_state_if_new_day(now_ist)
        date_key = now_ist.date()
        self._eod_processed_underlyings.setdefault(date_key, set()).update(
            u.upper() for u in underlyings
        )
        self._square_off_done = True

    # Return the set of underlyings already processed today.
    def eod_processed_underlyings(self, now: datetime) -> set[str]:
        now_ist = now.astimezone(IST)
        self._reset_eod_state_if_new_day(now_ist)
        return set(self._eod_processed_underlyings.get(now_ist.date(), set()))

    # Check if the current time is past the EOD window.
    def is_after_eod_window(self, now: datetime) -> bool:
        if not self.eod_end_time:
            return False
        now_ist = now.astimezone(IST)
        self._reset_eod_state_if_new_day(now_ist)
        return now_ist.time() > self.eod_end_time

    # Decide whether an EOD warning should be logged once per day.
    def should_log_eod_warning(self, now: datetime) -> bool:
        now_ist = now.astimezone(IST)
        if not self.is_after_eod_window(now):
            return False
        return self._eod_warning_logged_date != now_ist.date()

    # Mark that the EOD warning was logged for today.
    def mark_eod_warning_logged(self, now: datetime) -> None:
        now_ist = now.astimezone(IST)
        self._reset_eod_state_if_new_day(now_ist)
        self._eod_warning_logged_date = now_ist.date()

    # Reset daily EOD state when the date changes.
    def _reset_eod_state_if_new_day(self, now_ist: datetime) -> None:
        if self._current_eod_date == now_ist.date():
            return
        self._current_eod_date = now_ist.date()
        self._square_off_done = False
        self._eod_processed_underlyings[now_ist.date()] = set()
        self._eod_warning_logged_date = None
