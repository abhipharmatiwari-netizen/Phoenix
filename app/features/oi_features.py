"""Pure OI feature builders for option-chain snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

from app.data.option_chain_provider import OptionQuote


@dataclass(frozen=True)
class OiWall:
    present: bool
    strike: int | None = None
    oi: int = 0
    neighbor_avg: float = 0.0
    multiple: float = 0.0


def build_oi_features(
    quotes: Sequence[OptionQuote],
    *,
    candidate_strike: int,
    option_type: str = "CE",
    decision_ts: datetime | None = None,
    underlying_ltp: float | None = None,
    wall_multiple: float = 2.0,
) -> dict[str, Any]:
    """Build deterministic OI features for one candidate strike."""
    rows = _normalized_rows(quotes, decision_ts=decision_ts)
    side = option_type.upper()
    call_oi = _oi_by_strike(rows, "CE")
    put_oi = _oi_by_strike(rows, "PE")
    side_oi = _oi_by_strike(rows, side)
    candidate_oi = side_oi.get(int(candidate_strike), 0)
    side_total = sum(side_oi.values())
    wall = detect_oi_wall(
        rows,
        candidate_strike=int(candidate_strike),
        option_type=side,
        wall_multiple=wall_multiple,
    )
    max_pain = max_pain_strike(rows)

    spot = underlying_ltp if underlying_ltp is not None else _first_underlying_ltp(rows)
    distance_to_max_pain = (
        None
        if max_pain is None or spot is None
        else float(spot) - float(max_pain)
    )
    candidate_distance_pct = (
        None
        if spot in (None, 0)
        else (float(candidate_strike) - float(spot)) / float(spot)
    )

    return {
        "candidate_strike": int(candidate_strike),
        "option_type": side,
        "candidate_oi": candidate_oi,
        "candidate_oi_share": _safe_ratio(candidate_oi, side_total),
        "pcr_total": total_pcr(rows),
        "pcr_strike": strike_pcr(rows, int(candidate_strike)),
        "max_pain": max_pain,
        "distance_to_max_pain": distance_to_max_pain,
        "candidate_distance_pct": candidate_distance_pct,
        "oi_concentration_top3": oi_concentration_ratio(rows, option_type=side, top_n=3),
        "oi_wall_present": wall.present,
        "oi_wall_strike": wall.strike,
        "oi_wall_multiple": wall.multiple,
        "oi_wall_neighbor_avg": wall.neighbor_avg,
        "ce_total_oi": sum(call_oi.values()),
        "pe_total_oi": sum(put_oi.values()),
    }


def total_pcr(quotes: Sequence[OptionQuote]) -> float | None:
    rows = _normalized_rows(quotes)
    return _safe_ratio(sum(_oi_by_strike(rows, "PE").values()), sum(_oi_by_strike(rows, "CE").values()))


def strike_pcr(quotes: Sequence[OptionQuote], strike: int) -> float | None:
    rows = _normalized_rows(quotes)
    call_oi = _oi_by_strike(rows, "CE").get(int(strike), 0)
    put_oi = _oi_by_strike(rows, "PE").get(int(strike), 0)
    return _safe_ratio(put_oi, call_oi)


def max_pain_strike(quotes: Sequence[OptionQuote]) -> int | None:
    rows = _normalized_rows(quotes)
    strikes = sorted({row.strike for row in rows})
    if not strikes:
        return None
    call_oi = _oi_by_strike(rows, "CE")
    put_oi = _oi_by_strike(rows, "PE")
    payouts: dict[int, int] = {}
    for settlement in strikes:
        payout = 0
        for strike, oi in call_oi.items():
            payout += max(settlement - strike, 0) * oi
        for strike, oi in put_oi.items():
            payout += max(strike - settlement, 0) * oi
        payouts[settlement] = payout
    return min(payouts, key=lambda strike: (payouts[strike], strike))


def oi_concentration_ratio(
    quotes: Sequence[OptionQuote],
    *,
    option_type: str,
    top_n: int = 3,
) -> float | None:
    rows = _normalized_rows(quotes)
    oi_values = sorted(_oi_by_strike(rows, option_type.upper()).values(), reverse=True)
    total = sum(oi_values)
    if total <= 0:
        return None
    return sum(oi_values[: max(1, int(top_n))]) / total


def detect_oi_wall(
    quotes: Sequence[OptionQuote],
    *,
    candidate_strike: int,
    option_type: str = "CE",
    wall_multiple: float = 2.0,
) -> OiWall:
    rows = _normalized_rows(quotes)
    side = option_type.upper()
    by_strike = _oi_by_strike(rows, side)
    if not by_strike:
        return OiWall(False)

    if side == "PE":
        candidate_strikes = [strike for strike in by_strike if strike <= int(candidate_strike)]
        candidate_strikes.sort(reverse=True)
    else:
        candidate_strikes = [strike for strike in by_strike if strike >= int(candidate_strike)]
        candidate_strikes.sort()

    for strike in candidate_strikes:
        oi = by_strike.get(strike, 0)
        neighbor_avg = _neighbor_avg(by_strike, strike)
        if neighbor_avg <= 0:
            continue
        multiple = oi / neighbor_avg
        if oi > 0 and multiple >= float(wall_multiple):
            return OiWall(
                present=True,
                strike=strike,
                oi=oi,
                neighbor_avg=neighbor_avg,
                multiple=multiple,
            )
    return OiWall(False)


def _normalized_rows(
    quotes: Iterable[OptionQuote],
    *,
    decision_ts: datetime | None = None,
) -> list[OptionQuote]:
    rows = [quote.normalized() for quote in quotes]
    if decision_ts is not None:
        decision = _aware_utc(decision_ts)
        leaked = [row.snapshot_ts for row in rows if row.snapshot_ts > decision]
        if leaked:
            raise ValueError("option-chain feature input contains future snapshots")
    return rows


def _oi_by_strike(rows: Sequence[OptionQuote], option_type: str) -> dict[int, int]:
    side = option_type.upper()
    out: dict[int, int] = {}
    for row in rows:
        if row.option_type != side:
            continue
        out[row.strike] = out.get(row.strike, 0) + max(0, int(row.oi or 0))
    return out


def _neighbor_avg(oi_by_strike: dict[int, int], strike: int) -> float:
    strikes = sorted(oi_by_strike)
    try:
        idx = strikes.index(strike)
    except ValueError:
        return 0.0
    neighbor_oi = []
    if idx > 0:
        neighbor_oi.append(oi_by_strike[strikes[idx - 1]])
    if idx < len(strikes) - 1:
        neighbor_oi.append(oi_by_strike[strikes[idx + 1]])
    if not neighbor_oi:
        return 0.0
    return sum(neighbor_oi) / len(neighbor_oi)


def _safe_ratio(numerator: int | float, denominator: int | float) -> float | None:
    if denominator == 0:
        return None
    return float(numerator) / float(denominator)


def _first_underlying_ltp(rows: Sequence[OptionQuote]) -> float | None:
    for row in rows:
        if row.underlying_ltp is not None:
            return _to_float(row.underlying_ltp)
    return None


def _to_float(value: Decimal | float | int | str) -> float:
    return float(value)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "OiWall",
    "build_oi_features",
    "detect_oi_wall",
    "max_pain_strike",
    "oi_concentration_ratio",
    "strike_pcr",
    "total_pcr",
]
