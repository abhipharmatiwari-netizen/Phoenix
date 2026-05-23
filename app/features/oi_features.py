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
    raw_quotes = list(quotes)
    rows = _normalized_rows(raw_quotes, decision_ts=decision_ts)
    latest_rows = _latest_snapshot_rows(rows)
    side = option_type.upper()
    call_oi = _oi_by_strike(latest_rows, "CE")
    put_oi = _oi_by_strike(latest_rows, "PE")
    side_oi = _oi_by_strike(latest_rows, side)
    candidate_oi = side_oi.get(int(candidate_strike), 0)
    side_total = sum(side_oi.values())
    wall = detect_oi_wall(
        latest_rows,
        candidate_strike=int(candidate_strike),
        option_type=side,
        wall_multiple=wall_multiple,
    )
    max_pain = max_pain_strike(latest_rows)

    spot = underlying_ltp if underlying_ltp is not None else _first_underlying_ltp(latest_rows)
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
    candidate = _candidate_row(latest_rows, candidate_strike=int(candidate_strike), option_type=side)
    lineage = _timestamp_lineage(raw_quotes, decision_ts=decision_ts)
    missing = _missing_fields(candidate)
    spread_quality = _bid_ask_quality(candidate)

    return {
        **lineage,
        "candidate_strike": int(candidate_strike),
        "option_type": side,
        "candidate_oi": candidate_oi,
        "candidate_oi_velocity_per_minute": _oi_velocity_per_minute(
            rows,
            candidate_strike=int(candidate_strike),
            option_type=side,
        ),
        "candidate_oi_share": _safe_ratio(candidate_oi, side_total),
        "pcr_total": total_pcr(latest_rows),
        "pcr_strike": strike_pcr(latest_rows, int(candidate_strike)),
        "max_pain": max_pain,
        "distance_to_max_pain": distance_to_max_pain,
        "candidate_distance_pct": candidate_distance_pct,
        "oi_concentration_top3": oi_concentration_ratio(latest_rows, option_type=side, top_n=3),
        "oi_wall_present": wall.present,
        "oi_wall_strike": wall.strike,
        "oi_wall_multiple": wall.multiple,
        "oi_wall_neighbor_avg": wall.neighbor_avg,
        "oi_wall_persistence_snapshots": _wall_persistence_snapshots(
            rows,
            candidate_strike=int(candidate_strike),
            option_type=side,
            wall_strike=wall.strike,
            wall_multiple=wall_multiple,
        ),
        "oi_vs_spot_beta": _oi_vs_spot_beta(
            rows,
            candidate_strike=int(candidate_strike),
            option_type=side,
        ),
        "candidate_bid_ask_spread": spread_quality["spread"],
        "candidate_bid_ask_spread_pct": spread_quality["spread_pct"],
        "candidate_bid_ask_crossed": spread_quality["crossed"],
        "candidate_missing_fields": missing,
        "candidate_missing_fields_count": len(missing),
        "vix": _to_float(candidate.vix) if candidate and candidate.vix is not None else None,
        "vix_regime": _vix_regime(candidate),
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


def _latest_snapshot_rows(rows: Sequence[OptionQuote]) -> list[OptionQuote]:
    if not rows:
        return []
    latest_ts = max(row.snapshot_ts for row in rows)
    return [row for row in rows if row.snapshot_ts == latest_ts]


def _timestamp_lineage(
    rows: Sequence[OptionQuote],
    *,
    decision_ts: datetime | None,
) -> dict[str, Any]:
    decision = _aware_utc(decision_ts) if decision_ts is not None else None
    source_values = [_aware_utc(row.source_ts) for row in rows if row.source_ts is not None]
    ingested_values = [_aware_utc(row.ingested_at) for row in rows if row.ingested_at is not None]
    max_source_ts = max(source_values) if source_values else None
    max_ingested_at = max(ingested_values) if ingested_values else None
    return {
        "decision_ts": decision.isoformat() if decision else None,
        "max_source_ts": max_source_ts.isoformat() if max_source_ts else None,
        "max_ingested_at": max_ingested_at.isoformat() if max_ingested_at else None,
        "max_source_lag_seconds": (
            (decision - max_source_ts).total_seconds()
            if decision is not None and max_source_ts is not None
            else None
        ),
        "max_ingested_lag_seconds": (
            (decision - max_ingested_at).total_seconds()
            if decision is not None and max_ingested_at is not None
            else None
        ),
    }


def _candidate_row(
    rows: Sequence[OptionQuote],
    *,
    candidate_strike: int,
    option_type: str,
) -> OptionQuote | None:
    side = option_type.upper()
    for row in rows:
        if row.strike == int(candidate_strike) and row.option_type == side:
            return row
    return None


def _missing_fields(row: OptionQuote | None) -> list[str]:
    if row is None:
        return ["candidate_quote"]
    fields = (
        "source_ts",
        "trading_symbol",
        "exchange",
        "symbol_token",
        "oi",
        "volume",
        "bid",
        "ask",
        "ltp",
        "underlying_ltp",
        "vix",
    )
    return [field for field in fields if getattr(row, field) in (None, "")]


def _bid_ask_quality(row: OptionQuote | None) -> dict[str, Any]:
    if row is None:
        return {"spread": None, "spread_pct": None, "crossed": False}
    bid = _to_float(row.bid) if row.bid is not None else None
    ask = _to_float(row.ask) if row.ask is not None else None
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return {"spread": None, "spread_pct": None, "crossed": False}
    spread = ask - bid
    mid = (ask + bid) / 2.0
    return {
        "spread": spread,
        "spread_pct": _safe_ratio(spread, mid),
        "crossed": ask < bid,
    }


def _oi_velocity_per_minute(
    rows: Sequence[OptionQuote],
    *,
    candidate_strike: int,
    option_type: str,
) -> float | None:
    candidates = [
        row
        for row in rows
        if row.strike == int(candidate_strike) and row.option_type == option_type.upper()
    ]
    candidates.sort(key=lambda row: row.snapshot_ts)
    if len(candidates) < 2:
        return None
    prev = candidates[-2]
    latest = candidates[-1]
    elapsed_minutes = (latest.snapshot_ts - prev.snapshot_ts).total_seconds() / 60.0
    if elapsed_minutes <= 0:
        return None
    return (int(latest.oi or 0) - int(prev.oi or 0)) / elapsed_minutes


def _wall_persistence_snapshots(
    rows: Sequence[OptionQuote],
    *,
    candidate_strike: int,
    option_type: str,
    wall_strike: int | None,
    wall_multiple: float,
) -> int:
    if wall_strike is None:
        return 0
    count = 0
    for snapshot_ts in sorted({row.snapshot_ts for row in rows}):
        snapshot_rows = [row for row in rows if row.snapshot_ts == snapshot_ts]
        wall = detect_oi_wall(
            snapshot_rows,
            candidate_strike=candidate_strike,
            option_type=option_type,
            wall_multiple=wall_multiple,
        )
        if wall.present and wall.strike == wall_strike:
            count += 1
    return count


def _oi_vs_spot_beta(
    rows: Sequence[OptionQuote],
    *,
    candidate_strike: int,
    option_type: str,
) -> float | None:
    pairs: list[tuple[float, float]] = []
    for row in sorted(rows, key=lambda item: item.snapshot_ts):
        if row.strike != int(candidate_strike) or row.option_type != option_type.upper():
            continue
        spot = _to_float(row.underlying_ltp) if row.underlying_ltp is not None else None
        if spot is None:
            continue
        pairs.append((spot, float(row.oi or 0)))
    if len(pairs) < 2:
        return None
    spot_values = [item[0] for item in pairs]
    oi_values = [item[1] for item in pairs]
    spot_mean = sum(spot_values) / len(spot_values)
    oi_mean = sum(oi_values) / len(oi_values)
    variance = sum((spot - spot_mean) ** 2 for spot in spot_values)
    if variance == 0:
        return None
    covariance = sum(
        (spot - spot_mean) * (oi - oi_mean)
        for spot, oi in zip(spot_values, oi_values)
    )
    return covariance / variance


def _vix_regime(row: OptionQuote | None) -> str | None:
    if row is None or row.vix in (None, ""):
        return None
    vix = _to_float(row.vix)
    if vix < 14:
        return "LOW"
    if vix < 20:
        return "NORMAL"
    return "HIGH"


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
