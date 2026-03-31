"""
Track executed option and future legs and keep their tokens subscribed.
Caches last traded prices for quick lookup and dashboards.
"""

from __future__ import annotations

import atexit
import json
import logging
from pathlib import Path
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.core.feature_flags import load_stability_feature_flags

logger = logging.getLogger(__name__)


class ExecutedTokensTracker:
    """
    Track all executed option/future legs and keep their tokens subscribed + LTP updated.

    - RiskManager tells us whenever a position is opened or closed.
    - multi_instrument_stream asks us which tokens must always stay subscribed.
    - The websocket tick loop calls .on_tick() so we cache the latest LTP per label.
    """

    # Initialize the in-memory token tracker.
    def __init__(
        self,
        *,
        persist_enabled: bool | None = None,
        state_path: str | None = None,
        flush_interval_s: float | None = None,
    ) -> None:
        self._lock = threading.Lock()
        # label -> { token, exchangeType, last_ltp }
        self._by_label: Dict[str, Dict[str, Any]] = {}
        self._persisted_last_ltp: Dict[str, Dict[str, Any]] = {}
        self._dirty = False
        self._last_flush_monotonic = 0.0
        flags = load_stability_feature_flags()
        self._persist_enabled = (
            flags.executed_tokens_persist_enabled
            if persist_enabled is None
            else bool(persist_enabled)
        )
        path_raw = (
            flags.executed_tokens_state_path if state_path is None else str(state_path)
        )
        self._state_path = Path(path_raw)
        interval_raw = (
            flags.executed_tokens_state_flush_interval_s
            if flush_interval_s is None
            else float(flush_interval_s)
        )
        self._flush_interval_s = max(0.1, float(interval_raw))
        if self._persist_enabled:
            self._load_state()

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _load_state(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning(
                "ExecutedTokensTracker state load skipped (invalid file): path=%s err=%s",
                self._state_path,
                exc,
            )
            return
        labels = raw.get("labels") if isinstance(raw, dict) else None
        if not isinstance(labels, dict):
            return
        loaded: Dict[str, Dict[str, Any]] = {}
        for label, payload in labels.items():
            if isinstance(payload, dict):
                ltp = self._coerce_float(payload.get("last_ltp"))
                ts = payload.get("last_ltp_ts")
            else:
                ltp = self._coerce_float(payload)
                ts = None
            if ltp is None:
                continue
            loaded[str(label)] = {"last_ltp": float(ltp), "last_ltp_ts": ts}
        with self._lock:
            self._persisted_last_ltp = loaded

    def _snapshot_for_persist(self) -> Dict[str, Any]:
        labels: Dict[str, Dict[str, Any]] = {}
        for label, info in self._by_label.items():
            labels[str(label)] = {
                "last_ltp": self._coerce_float(info.get("last_ltp")),
                "last_ltp_ts": info.get("last_ltp_ts"),
            }
        return {
            "version": 1,
            "saved_at_utc": datetime.now(timezone.utc).isoformat(),
            "labels": labels,
        }

    def _atomic_write_json(self, payload: Dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(self._state_path)

    def _flush_if_due(self, *, force: bool = False) -> None:
        if not self._persist_enabled:
            return
        payload: Dict[str, Any] | None = None
        with self._lock:
            if not self._dirty and not force:
                return
            now_mono = time.monotonic()
            if (
                not force
                and (now_mono - self._last_flush_monotonic) < self._flush_interval_s
            ):
                return
            payload = self._snapshot_for_persist()
            self._dirty = False
            self._last_flush_monotonic = now_mono
        try:
            self._atomic_write_json(payload)
        except Exception as exc:
            logger.warning(
                "ExecutedTokensTracker state flush failed: path=%s err=%s",
                self._state_path,
                exc,
            )
            with self._lock:
                self._dirty = True

    def flush(self) -> None:
        self._flush_if_due(force=True)

    # -------- Registration from RiskManager --------

    # Seed the tracker from restored open positions on startup.
    def bootstrap_from_positions(
        self,
        positions: Dict[str, Dict[str, Any]],
        instrument_meta: Dict[str, Dict[str, Any]],
    ) -> None:
        """Seed the tracker from restored open positions on startup."""
        with self._lock:
            self._by_label.clear()
            for label, pos in positions.items():
                meta = instrument_meta.get(label, {})
                token = pos.get("symboltoken") or meta.get("token")
                ex_type = meta.get("exchangeType")
                if not token or ex_type is None:
                    continue
                persisted = self._persisted_last_ltp.get(label, {})
                self._by_label[label] = {
                    "token": str(token),
                    "exchangeType": int(ex_type),
                    "last_ltp": persisted.get("last_ltp"),
                    "last_ltp_ts": persisted.get("last_ltp_ts"),
                }
            self._dirty = True
        self._flush_if_due()

    # Register a newly opened position so its token stays subscribed.
    def register_position(self, label: str, instrument_meta: Dict[str, Any]) -> None:
        """Called when a new position is opened."""
        token = instrument_meta.get("token")
        ex_type = instrument_meta.get("exchangeType")
        if not token or ex_type is None:
            return
        with self._lock:
            prev = self._by_label.get(label) or self._persisted_last_ltp.get(label, {})
            self._by_label[label] = {
                "token": str(token),
                "exchangeType": int(ex_type),
                "last_ltp": self._coerce_float(prev.get("last_ltp")),
                "last_ltp_ts": prev.get("last_ltp_ts"),
            }
            self._dirty = True
        self._flush_if_due()

    # Unregister a closed position so its token can be dropped.
    def unregister_position(self, label: str) -> None:
        """Called when a position is closed."""
        with self._lock:
            self._by_label.pop(label, None)
            self._dirty = True
        self._flush_if_due()

    # -------- Tick + subscription helpers --------

    # Update cached LTP when a new tick arrives.
    def on_tick(self, label: str, ltp: float) -> None:
        """Update cached LTP for a label if it is a tracked executed leg."""
        with self._lock:
            info = self._by_label.get(label)
            if info is not None:
                info["last_ltp"] = float(ltp)
                info["last_ltp_ts"] = datetime.now(timezone.utc).isoformat()
                self._dirty = True
        self._flush_if_due()

    # Extend a base token list with all currently executed-leg tokens.
    def extend_token_list(
        self, base_token_list: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Return a new token_list that also includes all executed-leg tokens.

        SmartWebSocketV2 expects:
            { "exchangeType": 1, "tokens": ["12345", "67890"] }
        """
        with self._lock:
            by_ex_type: Dict[int, List[str]] = {}

            # start from base token list
            for entry in base_token_list or []:
                ex_type = entry.get("exchangeType")
                if ex_type is None:
                    continue
                ex_type = int(ex_type)
                by_ex_type.setdefault(ex_type, [])
                for tok in entry.get("tokens", []):
                    t = str(tok)
                    if t not in by_ex_type[ex_type]:
                        by_ex_type[ex_type].append(t)

            # add executed-leg tokens
            for info in self._by_label.values():
                ex_type = int(info["exchangeType"])
                tok = str(info["token"])
                by_ex_type.setdefault(ex_type, [])
                if tok not in by_ex_type[ex_type]:
                    by_ex_type[ex_type].append(tok)

        return [
            {"exchangeType": ex_type, "tokens": sorted(tokens)}
            for ex_type, tokens in by_ex_type.items()
        ]

    # -------- Read API (optional) --------

    # Return the last cached LTP for a label, if any.
    def get_last_ltp(self, label: str) -> float | None:
        with self._lock:
            info = self._by_label.get(label)
            if not info:
                return None
            return info.get("last_ltp")

    # Return a snapshot map of token -> label for tracked executed legs.
    def get_token_label_map(self) -> Dict[str, str]:
        with self._lock:
            result: Dict[str, str] = {}
            for label, info in self._by_label.items():
                token = info.get("token")
                if not token:
                    continue
                result[str(token)] = str(label)
            return result


executed_tokens_tracker = ExecutedTokensTracker()
atexit.register(executed_tokens_tracker.flush)
