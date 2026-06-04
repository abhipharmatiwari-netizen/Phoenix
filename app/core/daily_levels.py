"""
Fetch and cache previous-day high/low/close levels for underlyings.
Used by strategies that need daily reference levels.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, Optional

from app.core.clock import IClock, SystemClock
from app.core.order_client import AngelOrderClient, _make_angel_connection
from app.core.rate_limiter import rate_limiter

logger = logging.getLogger(__name__)


@dataclass
class DailyLevels:
    trading_date: date
    pdh: float
    pdl: float
    pclose: float
    popen: float


class DailyLevelsCache:
    """
    Fetches and caches previous-day levels per instrument label.
    """

    # Initialize the cache with a historical data client and lookback window.
    def __init__(
        self,
        historical_client: AngelOrderClient,
        *,
        lookback_days: int = 7,
        clock: Optional[IClock] = None,
    ) -> None:
        self._client = historical_client
        self._lookback_days = max(1, lookback_days)
        self._clock: IClock = clock or SystemClock()
        self._levels: Dict[str, DailyLevels] = {}
        self._loaded_for_date: Optional[date] = None

    # Refresh cached levels for a list of instruments if needed.
    def refresh_for_instruments(self, instruments: Iterable[Dict[str, Any]]) -> None:
        today = self._clock.now_utc().date()
        if self._loaded_for_date == today and self._levels:
            return

        self._levels.clear()
        from_dt = today - timedelta(days=self._lookback_days)
        to_dt = today
        loaded_any = False

        for inst in instruments:
            label = str(inst.get("label") or "")
            exchange = inst.get("exchange") or inst.get("segment") or inst.get("exch_seg")
            token = inst.get("token")
            kind = str(inst.get("kind") or "").upper()
            if kind and kind != "UNDERLYING":
                continue
            allowed_underlyings = {
                "NIFTY_IDX",
                "BANKNIFTY_IDX",
                "FINNIFTY_IDX",
                "SENSEX_IDX",
                "MIDCPNIFTY_IDX",
            }
            if label.upper() not in allowed_underlyings:
                continue
            if not label or not exchange or token is None:
                logger.debug("Skipping instrument without exchange/token: %s", inst)
                continue
            try:
                levels = self._fetch_last_daily_candle(
                    exch_seg=str(exchange),
                    token=str(token),
                    from_date=from_dt,
                    to_date=to_dt,
                )
            except Exception as exc:
                logger.warning("Failed to fetch daily levels for %s: %s", label, exc)
                continue
            if levels is None:
                continue
            self._levels[label] = levels
            loaded_any = True
            logger.info(
                "Loaded daily levels for %s: pdh=%.3f pdl=%.3f pclose=%.3f",
                label,
                levels.pdh,
                levels.pdl,
                levels.pclose,
            )

        if loaded_any:
            self._loaded_for_date = today

    # Return cached levels for a label, if available.
    def get_for_label(self, label: str) -> Optional[DailyLevels]:
        return self._levels.get(label)

    # Fetch the most recent completed daily candle and convert to levels.
    def _fetch_last_daily_candle(
        self,
        *,
        exch_seg: str,
        token: str,
        from_date: date,
        to_date: date,
    ) -> Optional[DailyLevels]:
        """
        Calls Angel historical API for ONE_DAY candles and returns the latest completed day.
        """
        payload = {
            "exchange": exch_seg,
            "symboltoken": str(token),
            "interval": "ONE_DAY",
            "fromdate": f"{from_date.isoformat()} 09:00",
            "todate": f"{to_date.isoformat()} 15:30",
        }

        conn = _make_angel_connection()
        body = json.dumps(payload)
        rate_limiter.acquire("historical")
        conn.request(
            "POST",
            "/rest/secure/angelbroking/historical/v1/getCandleData",
            body=body,
            headers=self._client._headers(),  # type: ignore[attr-defined]
        )
        res = conn.getresponse()
        text = res.read().decode("utf-8")
        if res.status != 200:
            logger.warning(
                "Historical fetch failed for %s:%s http=%s body=%s",
                exch_seg,
                token,
                res.status,
                text[:200],
            )
            return None
        if not text:
            logger.warning("Historical fetch empty response for %s:%s", exch_seg, token)
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(
                "Historical fetch invalid JSON for %s:%s body=%s",
                exch_seg,
                token,
                text[:200],
            )
            return None
        if not data.get("status"):
            logger.warning("Historical fetch failed for %s:%s => %s", exch_seg, token, data)
            return None
        data_block = data.get("data", {})
        candles = data_block.get("candles") if isinstance(data_block, dict) else data_block
        if not candles:
            return None
        last = candles[-1]
        try:
            ts_raw, o, h, low_, c, *_rest = last
            if isinstance(ts_raw, (int, float)):
                dt = datetime.fromtimestamp(ts_raw)
            else:
                dt = datetime.fromisoformat(str(ts_raw))
            return DailyLevels(
                trading_date=dt.date(),
                pdh=float(h),
                pdl=float(low_),
                pclose=float(c),
                popen=float(o),
            )
        except Exception as exc:
            logger.warning("Malformed candle for %s:%s -> %s (%s)", exch_seg, token, last, exc)
            return None


__all__ = ["DailyLevels", "DailyLevelsCache"]
