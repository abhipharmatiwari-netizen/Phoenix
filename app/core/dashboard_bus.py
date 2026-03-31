"""
In-memory, thread-safe bus for dashboard state and live data.
Stores ticks, indicators, positions, and PnL snapshots for the UI.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DashboardBus:
    """
    Thread-safe in-memory bus for feeding the dashboard with live ticks,
    indicator trends, open positions, trades, and P&L.
    """

    # Initialize dashboard state and thread lock.
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode = "UVICORN"
        self._trade_mode = "PAPER"
        self._live = False
        self._instrument_meta: Dict[str, Dict[str, Any]] = {}
        self._last_ticks: Dict[str, Dict[str, Any]] = {}
        # indicators[label][timeframe] -> {"atr": [...], "rsi": [...], "macd": [...]}
        self._indicators: Dict[str, Dict[int, Dict[str, List[float]]]] = {}
        self._open_positions: Dict[str, Dict[str, Any]] = {}
        self._recent_trades: List[Dict[str, Any]] = []
        self._realized_pnl: float = 0.0
        self._strategy_selection: Dict[str, Dict[str, Any]] = {}
        self._last_sent_snapshot: Dict[str, Any] = {}
        self._delta_call_count: int = 0
        self._delta_full_interval: int = 30

    # Set runtime mode label for the dashboard.
    def set_mode(self, mode: str) -> None:
        with self._lock:
            self._mode = (mode or "UVICORN").upper()

    # Set trade mode label (paper/live) for the dashboard.
    def set_trade_mode(self, trade_mode: str) -> None:
        with self._lock:
            self._trade_mode = (trade_mode or "PAPER").upper()

    # Mark whether the system is running live trading.
    def set_live(self, live: bool) -> None:
        with self._lock:
            self._live = bool(live)

    # Store instrument metadata (lot sizes, tokens) for later use.
    def set_instrument_meta(self, meta: Dict[str, Dict[str, Any]]) -> None:
        with self._lock:
            self._instrument_meta = dict(meta or {})

    # Upsert a single instrument metadata row without replacing the full map.
    def upsert_instrument_meta(self, label: str, meta: Dict[str, Any]) -> None:
        key = str(label or "").strip()
        if not key:
            return
        with self._lock:
            merged = dict(self._instrument_meta.get(key, {}))
            merged.update(dict(meta or {}))
            self._instrument_meta[key] = merged

    # Resolve instrument metadata by symbol or token (thread-safe snapshot lookup).
    def resolve_instrument_meta(
        self,
        *,
        symbol: Optional[str] = None,
        token: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        sym = str(symbol or "").upper()
        tok = str(token or "")
        if not sym and not tok:
            return None
        with self._lock:
            for meta in self._instrument_meta.values():
                if tok:
                    meta_token = (
                        meta.get("token")
                        or meta.get("symboltoken")
                        or meta.get("instrument_token")
                    )
                    if meta_token is not None and str(meta_token) == tok:
                        return dict(meta)
                if sym:
                    meta_symbol = str(meta.get("symbol") or "").upper()
                    if meta_symbol and meta_symbol == sym:
                        return dict(meta)
        return None

    # Resolve a dashboard label from broker symbol/token.
    def resolve_label(
        self,
        *,
        symbol: Optional[str] = None,
        token: Optional[str] = None,
    ) -> Optional[str]:
        sym = str(symbol or "").upper()
        tok = str(token or "")
        if not sym and not tok:
            return None
        with self._lock:
            for label, meta in self._instrument_meta.items():
                if tok:
                    meta_token = (
                        meta.get("token")
                        or meta.get("symboltoken")
                        or meta.get("instrument_token")
                    )
                    if meta_token is not None and str(meta_token) == tok:
                        return str(label)
                if sym:
                    meta_symbol = str(meta.get("symbol") or "").upper()
                    if meta_symbol and meta_symbol == sym:
                        return str(label)
        return None

    # Update realized PnL total shown on the dashboard.
    def set_realized_pnl(self, realized: float) -> None:
        with self._lock:
            self._realized_pnl = float(realized)

    # Return the latest cached price for a label, if available.
    def get_last_price(self, label: str) -> Optional[float]:
        with self._lock:
            tick = self._last_ticks.get(label)
            if not tick:
                return None
            try:
                return float(tick.get("price"))
            except (TypeError, ValueError):
                return None

    # Resolve latest price for a broker symbol/token via the dashboard label map.
    def get_last_price_for_instrument(
        self,
        *,
        symbol: Optional[str] = None,
        token: Optional[str] = None,
    ) -> Optional[float]:
        label = self.resolve_label(symbol=symbol, token=token)
        if not label:
            return None
        return self.get_last_price(label)

    # Return latest raw tick payload for a label.
    def get_last_tick(self, label: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            tick = self._last_ticks.get(label)
            if not tick:
                return None
            return dict(tick)

    # Upsert current strategy selection state for an underlying.
    def upsert_strategy_selection(
        self,
        underlying: str,
        *,
        regime: str,
        selected_strategies: List[str],
        reason: str,
        hold_until: Optional[str],
        updated_at: Optional[str] = None,
    ) -> None:
        key = str(underlying or "").strip().upper()
        if not key:
            return
        with self._lock:
            self._strategy_selection[key] = {
                "underlying": key,
                "regime": str(regime or "").strip().upper(),
                "selected_strategies": list(selected_strategies or []),
                "reason": str(reason or ""),
                "hold_until": str(hold_until) if hold_until else None,
                "updated_at": (
                    str(updated_at)
                    if updated_at
                    else datetime.now(timezone.utc).isoformat()
                ),
            }

    # Return a defensive copy of active selection state by underlying.
    def strategy_selection_snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {
                key: dict(value)
                for key, value in self._strategy_selection.items()
            }

    # Load existing open positions into the dashboard state.
    def bootstrap_positions(self, positions: Dict[str, Dict[str, Any]]) -> None:
        with self._lock:
            self._open_positions = dict(positions or {})

    # Record the latest tick price and timestamp for a label.
    def record_tick(self, label: str, price: float, ts: datetime) -> None:
        with self._lock:
            tick = self._last_ticks.setdefault(
                label,
                {"open": price, "price": price, "ts": ts},
            )
            tick["price"] = price
            tick.setdefault("open", price)
            tick["ts"] = ts

    # Record a closed bar and indicator values for a timeframe.
    def record_bar(
        self,
        label: str,
        timeframe_seconds: int,
        candle,
        indicators: Dict[str, Any],
    ) -> None:
        with self._lock:
            tf = self._indicators.setdefault(label, {}).setdefault(
                int(timeframe_seconds),
                {"atr": [], "rsi": [], "macd": []},
            )
            if indicators.get("atr") is not None:
                self._append_bounded(tf["atr"], float(indicators["atr"]))
            if indicators.get("rsi") is not None:
                self._append_bounded(tf["rsi"], float(indicators["rsi"]))
            if indicators.get("macd") is not None:
                self._append_bounded(tf["macd"], float(indicators["macd"]))

    # Add or update an open position record.
    def add_position(
        self,
        label: str,
        *,
        side: str,
        qty: int,
        entry_price: float,
        lot_size: Optional[float],
        template_name: Optional[str],
        role: Optional[str],
        underlying: Optional[str],
        strategy_name: Optional[str],
        reason: Optional[str],
        entry_ts: str,
    ) -> None:
        with self._lock:
            resolved_lot_size = (
                lot_size
                if lot_size not in (None, 0, 0.0)
                else self._instrument_meta.get(label, {}).get("lot_size", 1)
            )
            self._open_positions[label] = {
                "side": side.upper(),
                "qty": qty,
                "entry_price": entry_price,
                "template_name": template_name,
                "role": role,
                "underlying": underlying,
                "strategy_name": strategy_name,
                "reason": reason,
                "entry_ts": entry_ts,
                "lot_size": resolved_lot_size,
            }

    # Remove a position and record trade info and PnL.
    def close_position(
        self,
        label: str,
        *,
        exit_price: float,
        realized_pnl: Optional[float] = None,
        trade_record: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            pos = self._open_positions.pop(label, None)
            pnl = None
            if pos:
                lot_size = (
                    pos.get("lot_size")
                    or self._instrument_meta.get(label, {}).get("lot_size", 1)
                )
                side_mult = 1.0 if pos.get("side", "SELL").upper() == "BUY" else -1.0
                qty = int(pos.get("qty", 0))
                entry_price = float(pos.get("entry_price", 0.0))
                pnl = (exit_price - entry_price) * side_mult * qty * float(lot_size or 1)
            if realized_pnl is not None:
                self._realized_pnl = float(realized_pnl)
            trade_payload = trade_record or {}
            if trade_record is None and pos:
                trade_payload = {
                    "label": label,
                    "underlying": pos.get("underlying"),
                    "side": pos.get("side"),
                    "qty": pos.get("qty"),
                    "entry_price": pos.get("entry_price"),
                    "exit_price": exit_price,
                    "pnl": pnl,
                    "template_name": pos.get("template_name"),
                    "strategy_name": pos.get("strategy_name"),
                    "reason": pos.get("reason"),
                    "exit_ts": datetime.now(timezone.utc).isoformat(),
                    "entry_ts": pos.get("entry_ts"),
                }
            if trade_payload:
                self._recent_trades.append(trade_payload)
                self._recent_trades = self._recent_trades[-50:]

    # Build a full snapshot payload for the dashboard UI.
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            instruments = []
            for label, tick in self._last_ticks.items():
                tf_data = self._pick_timeframe(label)
                change_pct = 0.0
                try:
                    open_px = float(tick.get("open", tick.get("price", 0.0)) or 0.0)
                    if open_px:
                        change_pct = (float(tick["price"]) - open_px) / open_px * 100
                except Exception:
                    change_pct = 0.0

                raw_ts = tick.get("ts", now)
                if isinstance(raw_ts, datetime):
                    tick_time = raw_ts.isoformat()
                else:
                    tick_time = str(raw_ts)

                instruments.append(
                    {
                        "label": label,
                        "price": round(float(tick["price"]), 2),
                        "change_pct": round(change_pct, 2),
                        # Always serialize tick time as a string to avoid JSON datetime errors
                        "tick_time": tick_time,
                        "atr_series": tf_data.get("atr", []),
                        "rsi_series": tf_data.get("rsi", []),
                        "macd_series": tf_data.get("macd", []),
                    }
                )

            pnl_by_instrument: Dict[str, float] = {}
            open_positions_payload: List[Dict[str, Any]] = []
            open_pnl = 0.0
            for label, pos in self._open_positions.items():
                last_price = float(
                    self._last_ticks.get(label, {}).get(
                        "price", pos.get("entry_price", 0.0)
                    )
                )
                qty = float(pos.get("qty", 0))
                lot_size = float(
                    pos.get("lot_size")
                    or self._instrument_meta.get(label, {}).get("lot_size", 1)
                    or 1
                )
                qty_lots = qty
                side_mult = 1.0 if pos.get("side", "SELL").upper() == "BUY" else -1.0
                pnl = (
                    (last_price - float(pos.get("entry_price", 0.0)))
                    * side_mult
                    * qty
                    * float(lot_size or 1)
                )
                pnl_by_instrument[label] = round(
                    pnl_by_instrument.get(label, 0.0) + pnl, 2
                )
                open_pnl += pnl
                open_positions_payload.append(
                    {
                        "instrument": label,
                        "side": pos.get("side"),
                        "qty": pos.get("qty"),
                        "qty_lots": qty_lots,
                        "lot_size": lot_size,
                        "entry": float(pos.get("entry_price", 0.0)),
                        "last": last_price,
                        "pnl": round(pnl, 2),
                        "template": pos.get("template_name"),
                    }
                )

            recent_trades = list(self._recent_trades[-20:])
            total_pnl = round(self._realized_pnl + open_pnl, 2)

            # Diagnostic logging for P&L mismatch detection
            num_positions = len(self._open_positions)
            if num_positions > 0:
                logger.debug(
                    "[DASHBOARD_PNL] SUMMARY: positions=%d, realized=%.2f, open=%.2f, total=%.2f, instruments=%d",
                    num_positions,
                    self._realized_pnl,
                    open_pnl,
                    total_pnl,
                    len(pnl_by_instrument)
                )
                # Log each position for verification
                for label, pos in self._open_positions.items():
                    last_price = float(self._last_ticks.get(label, {}).get("price", pos.get("entry_price", 0.0)))
                    entry_price = float(pos.get("entry_price", 0.0))
                    side = pos.get("side", "SELL").upper()
                    qty = float(pos.get("qty", 0))
                    side_mult = 1.0 if side == "BUY" else -1.0
                    pos_pnl = (
                        (last_price - entry_price)
                        * side_mult
                        * qty
                        * float(
                            pos.get("lot_size")
                            or self._instrument_meta.get(label, {}).get("lot_size", 1)
                            or 1
                        )
                    )
                    logger.debug(
                        "[DASHBOARD_PNL_POS] label=%s side=%s qty=%d entry=%.2f last=%.2f pnl=%.2f",
                        label,
                        side,
                        int(qty),
                        entry_price,
                        last_price,
                        pos_pnl,
                    )
            # Alert if significant P&L variation detected (> ₹50k)
            if abs(open_pnl) > 50000 or abs(total_pnl) > 50000:
                logger.warning(
                    "[DASHBOARD_PNL_ALERT] High P&L detected: total=%.2f (realized=%.2f + open=%.2f)",
                    total_pnl,
                    self._realized_pnl,
                    open_pnl
                )

            return {
                "mode": self._mode,
                "trade_mode": self._trade_mode,
                "live": self._live,
                "timestamp": now,
                "instruments": instruments,
                "trades": {
                    "recent": recent_trades,
                    "open_positions": open_positions_payload,
                },
                "pnl": {
                    "by_instrument": pnl_by_instrument,
                    "total": total_pnl,
                    "realized": round(self._realized_pnl, 2),
                    "open": round(open_pnl, 2),
                },
                "strategy_selection": [
                    dict(value)
                    for key, value in sorted(self._strategy_selection.items())
                ],
            }

    # Compute a delta snapshot containing only changed keys since the last send.
    def delta_snapshot(self, full_interval: Optional[int] = None) -> Dict[str, Any]:
        """
        Return only the keys that changed since the last sent snapshot.
        Every *full_interval*-th call (default 30), return a full snapshot
        for compaction/resync.  The returned dict always contains a ``_mode``
        key: either ``"delta"`` or ``"full_snapshot"``.
        """
        interval = full_interval if full_interval is not None else self._delta_full_interval
        self._delta_call_count += 1

        current = self.snapshot()

        # Periodically send a full snapshot for compaction / client resync.
        if self._delta_call_count % interval == 0 or not self._last_sent_snapshot:
            self._last_sent_snapshot = current
            return {**current, "_mode": "full_snapshot"}

        # Build delta: include only top-level keys whose values differ.
        delta: Dict[str, Any] = {"_mode": "delta"}
        for key, value in current.items():
            prev = self._last_sent_snapshot.get(key)
            if value != prev:
                delta[key] = value

        self._last_sent_snapshot = current
        return delta

    # Backward-compatible alias for snapshot().
    def get_state(self) -> Dict[str, Any]:
        """Backward compatibility alias for snapshot()."""
        return self.snapshot()

    # Pick the most suitable indicator timeframe to display.
    def _pick_timeframe(self, label: str) -> Dict[str, List[float]]:
        frames = self._indicators.get(label, {})
        if not frames:
            return {"atr": [], "rsi": [], "macd": []}
        # Prefer the smallest available timeframe so trends show up as soon as a bar closes.
        preferred_order = [60, 300, 900]
        for tf in preferred_order:
            if tf in frames:
                return frames[tf]
        min_tf = min(frames.keys())
        return frames[min_tf]

    # Append a value while keeping only the most recent entries.
    def _append_bounded(self, arr: List[float], value: float, limit: int = 120) -> None:
        arr.append(value)
        if len(arr) > limit:
            del arr[: len(arr) - limit]


dashboard_bus = DashboardBus()
