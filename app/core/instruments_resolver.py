# instruments_resolver.py
# Resolve instruments and option chains from the Angel scrip master.
# Provides helpers to pick futures and strikes for indices and commodities.

import logging
from typing import Any, Dict, Iterable, Optional, Union

import pandas as pd
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
_SCRIP_MASTER_CACHE: Dict[str, Any] = {"date": None, "df": None}


def _coerce_tick_size(raw: Any) -> Optional[float]:
    try:
        tick_size = float(raw)
    except (TypeError, ValueError):
        return None
    if tick_size <= 0:
        return None
    if tick_size >= 1.0:
        tick_size /= 100.0
    return tick_size


def _tick_size_from_row(row: pd.Series) -> Optional[float]:
    for field in (
        "tick_size",
        "tickSize",
        "ticksize",
        "price_step",
        "priceStep",
        "pricestep",
        "min_tick",
    ):
        if field in row.index:
            tick_size = _coerce_tick_size(row[field])
            if tick_size is not None:
                return tick_size
    return None


def _lot_size_from_row(row: pd.Series) -> Optional[int]:
    for field in ("lotsize", "lot_size", "lotsize1"):
        if field in row.index:
            try:
                return int(row[field])
            except Exception:
                return None
    return None


# ---------- CORE HELPERS ----------


# Download and cache the scrip master file as a DataFrame.
def load_scrip_master() -> pd.DataFrame:
    """
    Download the OpenAPIScripMaster once and return as DataFrame.
    Reuse this across futures / options / scanners.
    """
    today = datetime.now().strftime("%Y%m%d")
    if _SCRIP_MASTER_CACHE["date"] == today and _SCRIP_MASTER_CACHE["df"] is not None:
        logger.info("Using in-memory ScripMaster cache for %s", today)
        return _SCRIP_MASTER_CACHE["df"]

    cache_dir = Path(__file__).resolve().parents[2] / "logs"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"scrip_master_{today}.json"

    if cache_path.exists():
        logger.info("Using cached ScripMaster for %s: %s", today, cache_path)
        df = pd.read_json(cache_path)
        _SCRIP_MASTER_CACHE.update({"date": today, "df": df})
        _prune_old_scrip_master_files(cache_dir, keep_days=7)
        return df

    logger.info("Downloading ScripMaster fresh for %s...", today)
    df = pd.read_json(SCRIP_MASTER_URL)
    try:
        df.to_json(cache_path, orient="records")
        logger.info("Cached ScripMaster at %s", cache_path)
    except Exception:
        logger.warning("Failed to cache ScripMaster locally", exc_info=True)
    _SCRIP_MASTER_CACHE.update({"date": today, "df": df})
    _prune_old_scrip_master_files(cache_dir, keep_days=7)
    return df


def _prune_old_scrip_master_files(cache_dir: Path, *, keep_days: int = 7) -> None:
    """Delete scrip_master_{date}.json files older than keep_days to prevent unbounded growth."""
    import time as _time
    cutoff = _time.time() - keep_days * 86400
    try:
        for old in cache_dir.glob("scrip_master_*.json"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
                    logger.debug("Pruned stale scrip master cache: %s", old.name)
            except Exception:
                pass
    except Exception:
        pass


# Pick the nearest-expiry future for a symbol pattern.
def get_front_future_for_symbol(
    df: pd.DataFrame,
    symbol_pattern: str,
    *,
    segments: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """
    Pick nearest-expiry future for a given symbol pattern and allowed segments.
    Useful for additional commodities (e.g., SILVER, CRUDE) in future phases.
    """
    segments = set(seg.upper() for seg in (segments or ["MCX", "NCO"]))
    df = df.copy()
    futs = df[
        df["symbol"].astype(str).str.contains(symbol_pattern, na=False)
        & df["symbol"].astype(str).str.endswith("FUT")
        & df["exch_seg"].astype(str).str.upper().isin(segments)
    ].copy()

    if futs.empty:
        raise RuntimeError(f"No {symbol_pattern} futures found in ScripMaster")

    futs["expiry_dt"] = pd.to_datetime(
        futs["expiry"].astype(str).str.strip(),
        format="%d%b%Y",  # e.g., "24NOV2025"
        errors="coerce",
    )
    today = pd.Timestamp.today().normalize()
    futs = futs[futs["expiry_dt"] >= today]

    if futs.empty:
        raise RuntimeError(f"No future-dated {symbol_pattern} futures found")

    front = futs.sort_values("expiry_dt").iloc[0]
    info = {
        "symbol": str(front["symbol"]),
        "expiry": str(front["expiry"]),
        "expiry_dt": front["expiry_dt"],
        "exch_seg": str(front["exch_seg"]),
        "token": str(front["token"]),
        "tick_size": _tick_size_from_row(front),
    }

    logger.info(
        "Front FUT for %s: symbol=%s expiry=%s exch=%s token=%s",
        symbol_pattern,
        info["symbol"],
        info["expiry"],
        info["exch_seg"],
        info["token"],
    )
    return info


# Backward-compatible NATURALGAS front future helper.
def get_front_future(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Backward-compatible NATURALGAS wrapper.
    """
    return get_front_future_for_symbol(df, "NATURALGAS", segments=["MCX", "NCO"])


# Select ATM CE/PE options for NATURALGAS based on FUT LTP.
def get_atm_options(
    df: pd.DataFrame,
    fut_meta: Dict[str, Any],
    underlying_ltp: float,
) -> Dict[str, Any]:
    """
    Given ScripMaster DF, FUT meta (from get_front_future), and FUT LTP,
    select NATURALGAS ATM CE + PE options:

    - First tries same expiry DATE as FUT
    - If not found, falls back to nearest available options expiry date
    """
    df = df.copy()
    # df["expiry_dt"] = pd.to_datetime(df["expiry"], errors="coerce")
    df["expiry_dt"] = pd.to_datetime(
        df["expiry"].astype(str).str.strip(),
        format="%d%b%Y",  # only if expiry strings are like "24NOV2025"
        errors="coerce",
    )

    target_date = pd.to_datetime(fut_meta["expiry_dt"]).date()
    logger.info("Looking for NATURALGAS options with expiry date = %s", target_date)

    # Base: all NATURALGAS options (CE/PE) with valid expiry on MCX-like segments
    base = df[
        df["symbol"].astype(str).str.contains("NATURALGAS", na=False)
        & (
            df["symbol"].astype(str).str.endswith("CE")
            | df["symbol"].astype(str).str.endswith("PE")
        )
    ].copy()

    # base["expiry_dt"] = pd.to_datetime(base["expiry"], errors="coerce")
    base["expiry_dt"] = pd.to_datetime(
        base["expiry"].astype(str).str.strip(),
        format="%d%b%Y",  # only if expiry strings are like "24NOV2025"
        errors="coerce",
    )

    base = base[base["expiry_dt"].notna() & base["exch_seg"].isin(["MCX", "NCO"])]

    if base.empty:
        raise RuntimeError("No NATURALGAS options found at all in ScripMaster")

    # ---- 1) Try exact same expiry DATE as FUT ----
    base_dates = base["expiry_dt"].dt.date
    exact = base[base_dates == target_date].copy()

    if exact.empty:
        # ---- 2) Fallback: nearest available options expiry date ----
        available_dates = sorted({d for d in base_dates.dropna()})
        logger.warning(
            "No NATURALGAS options found for exact expiry %s. "
            "Available option expiries: %s",
            target_date,
            ", ".join(d.strftime("%Y-%m-%d") for d in available_dates),
        )

        # Choose the date with minimum absolute distance from target_date
        nearest_date = min(available_dates, key=lambda d: abs(d - target_date))
        logger.warning(
            "Falling back to nearest options expiry date: %s",
            nearest_date.strftime("%Y-%m-%d"),
        )

        opts = base[base["expiry_dt"].dt.date == nearest_date].copy()
    else:
        opts = exact

    # Strike is in paise; convert to rupees
    opts["strike_val"] = pd.to_numeric(opts["strike"], errors="coerce") / 100.0
    opts = opts[opts["strike_val"].notna()]
    if opts.empty:
        raise RuntimeError("No valid strike values for NATURALGAS options")

    ce = opts[opts["symbol"].astype(str).str.endswith("CE")].copy()
    pe = opts[opts["symbol"].astype(str).str.endswith("PE")].copy()

    if ce.empty or pe.empty:
        raise RuntimeError(
            f"Missing CE or PE options in NATURALGAS chain for FUT expiry {target_date}"
        )

    ce["dist"] = (ce["strike_val"] - underlying_ltp).abs()
    pe["dist"] = (pe["strike_val"] - underlying_ltp).abs()

    atm_ce = ce.sort_values("dist").iloc[0]
    atm_pe = pe.sort_values("dist").iloc[0]

    logger.info(
        "Selected NG ATM CE: symbol=%s strike=%.2f token=%s",
        atm_ce["symbol"],
        atm_ce["strike_val"],
        atm_ce["token"],
    )

    logger.info(
        "Selected NG ATM PE: symbol=%s strike=%.2f token=%s",
        atm_pe["symbol"],
        atm_pe["strike_val"],
        atm_pe["token"],
    )

    lot_size = _lot_size_from_row(atm_ce)

    return {
        "ce_token": str(atm_ce["token"]),
        "pe_token": str(atm_pe["token"]),
        "ce_strike": float(atm_ce["strike_val"]),
        "pe_strike": float(atm_pe["strike_val"]),
        "ce_symbol": str(atm_ce["symbol"]),
        "pe_symbol": str(atm_pe["symbol"]),
        "lot_size": lot_size,
        "ce_tick_size": _tick_size_from_row(atm_ce),
        "pe_tick_size": _tick_size_from_row(atm_pe),
    }


# ng_instruments.py (add below existing imports/helpers)


# Find the underlying index row in the scrip master.
def find_index_underlying(df: pd.DataFrame, base_symbol: str = "NIFTY") -> dict:
    """
    Find NIFTY 50 underlying in ScripMaster.
    Tries to locate an INDEX row whose symbol starts with 'NIFTY'.
    """
    idx = df[
        df["symbol"].astype(str).str.startswith(base_symbol, na=False)
        & df["exch_seg"].isin(["NSE", "INDICES"])
    ].copy()

    if idx.empty:
        raise RuntimeError(
            f"Could not find underlying index for {base_symbol} in ScripMaster"
        )

    # Pick the first (usually 'NIFTY 50' / 'NIFTY')
    row = idx.iloc[0]
    info = {
        "symbol": str(row["symbol"]),
        "exch_seg": str(row["exch_seg"]),
        "token": str(row["token"]),
    }
    logger.info(
        "Found index underlying: symbol=%s exch=%s token=%s",
        info["symbol"],
        info["exch_seg"],
        info["token"],
    )
    return info


# Pick ATM/OTM/ITM option strikes for an index or future underlying.
def pick_index_options_strikes(
    df: pd.DataFrame,
    base_symbol: str,
    index_ltp: float,
    *,
    steps_otm: Union[int, Iterable[int]] = 1,
    steps_itm: int = 1,
    segments: Optional[Iterable[str]] = None,
    expiry_dt: Optional[datetime] = None,
) -> dict:
    """
    Generic helper to pick ATM + OTM CE/PE options for an index (e.g. NIFTY) or a
    future underlying (e.g. NATURALGAS) with configurable segments.
    - Uses nearest expiry >= today (or the provided expiry date)
    - Uses 'strike' column (paise -> rupees) to define ATM/OTM
    - Segments default to NFO/NSEFO but can be overridden for MCX/NCO, etc.
    """
    base_symbol = base_symbol.upper()
    segments_upper = {seg.upper() for seg in (segments or ["NFO", "NSEFO"])}
    df = df.copy()
    #    df["expiry_dt"] = pd.to_datetime(df["expiry"], errors="coerce")
    df["expiry_dt"] = pd.to_datetime(
        df["expiry"].astype(str).str.strip(),
        format="%d%b%Y",  # only if expiry strings are like "24NOV2025"
        errors="coerce",
    )

    today = pd.Timestamp.today().normalize()

    # 1) Filter for options rows (CE/PE) for this symbol within allowed segments
    opts = df[
        df["symbol"].astype(str).str.startswith(base_symbol, na=False)
        & (
            df["symbol"].astype(str).str.endswith("CE")
            | df["symbol"].astype(str).str.endswith("PE")
        )
        & df["exch_seg"].astype(str).str.upper().isin(segments_upper)
    ].copy()

    opts = opts[opts["expiry_dt"].notna()]
    if expiry_dt is not None:
        target = pd.to_datetime(expiry_dt).normalize()
        opts["expiry_norm"] = opts["expiry_dt"].dt.normalize()
        exact = opts[opts["expiry_norm"] == target]
        if exact.empty:
            if opts.empty:
                raise RuntimeError(
                    f"No options found for {base_symbol} in segments {segments_upper}"
                )
            opts["expiry_distance"] = (opts["expiry_dt"] - target).abs()
            nearest_expiry = opts.loc[opts["expiry_distance"].idxmin(), "expiry_dt"]
            chain = opts[opts["expiry_dt"] == nearest_expiry].copy()
        else:
            nearest_expiry = exact["expiry_dt"].iloc[0]
            chain = exact.copy()
    else:
        opts = opts[opts["expiry_dt"] >= today]
        if opts.empty:
            raise RuntimeError(
                f"No options found for {base_symbol} in segments {segments_upper}"
            )
        nearest_expiry = opts["expiry_dt"].min()
        chain = opts[opts["expiry_dt"] == nearest_expiry].copy()

    chain["strike_val"] = pd.to_numeric(chain["strike"], errors="coerce") / 100.0
    chain = chain[chain["strike_val"].notna()]
    if chain.empty:
        raise RuntimeError(f"No valid strike values for {base_symbol} options")

    strikes = sorted(chain["strike_val"].unique())
    if len(strikes) >= 2:
        diffs = [b - a for a, b in zip(strikes[:-1], strikes[1:]) if (b - a) > 0]
        strike_step = min(diffs) if diffs else strikes[1] - strikes[0]
    else:
        strike_step = 50.0  # fallback

    if strike_step == 0:
        strike_step = 50.0

    # Normalize OTM step inputs into a unique list of ints.
    def _normalize_steps(value: Union[int, Iterable[int]]) -> list[int]:
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            steps: list[int] = []
            for v in value:
                try:
                    step = int(v)
                except Exception:
                    continue
                if step <= 0 or step in steps:
                    continue
                steps.append(step)
            return steps or [1]
        try:
            step = int(value)
        except Exception:
            return [1]
        return [step] if step > 0 else [1]

    steps_otm_list = _normalize_steps(steps_otm)

    # 3) Compute ATM / OTM / ITM target strikes
    atm_strike = round(index_ltp / strike_step) * strike_step
    itm_ce_strike = atm_strike - steps_itm * strike_step
    itm_pe_strike = atm_strike + steps_itm * strike_step

    # Find the closest available strike to a target value.
    def nearest_available_strike(target: float) -> float:
        return min(strikes, key=lambda s: abs(s - target))

    atm_strike = nearest_available_strike(atm_strike)
    itm_ce_strike = nearest_available_strike(itm_ce_strike)
    itm_pe_strike = nearest_available_strike(itm_pe_strike)

    # Pick the best matching option row for strike and CE/PE suffix.
    def pick_row(strike_val: float, suffix: str):
        sub = chain[
            (chain["strike_val"] == strike_val)
            & chain["symbol"].astype(str).str.endswith(suffix)
        ].copy()
        if sub.empty:
            chain["dist"] = (chain["strike_val"] - strike_val).abs()
            sub = chain[chain["symbol"].astype(str).str.endswith(suffix)].sort_values(
                "dist"
            )
        if sub.empty:
            raise RuntimeError(
                f"No {suffix} option found near strike {strike_val} for {base_symbol}"
            )
        r = sub.iloc[0]
        return {
            "token": str(r["token"]),
            "symbol": str(r["symbol"]),
            "strike": float(r["strike_val"]),
            "exch_seg": str(r.get("exch_seg", "")),
            "lot_size": _lot_size_from_row(r),
            "tick_size": _tick_size_from_row(r),
        }

    atm_ce = pick_row(atm_strike, "CE")
    atm_pe = pick_row(atm_strike, "PE")
    otm_pairs = []
    for step in steps_otm_list:
        otm_ce_strike = nearest_available_strike(atm_strike + step * strike_step)
        otm_pe_strike = nearest_available_strike(atm_strike - step * strike_step)
        otm_pairs.append(
            {
                "step": step,
                "ce": pick_row(otm_ce_strike, "CE"),
                "pe": pick_row(otm_pe_strike, "PE"),
            }
        )
    itm_ce = pick_row(itm_ce_strike, "CE")
    itm_pe = pick_row(itm_pe_strike, "PE")

    logger.info(
        "Index %s expiry=%s | ATM=%.2f | OTM steps=%s | ITM_CE=%.2f ITM_PE=%.2f",
        base_symbol,
        nearest_expiry.date(),
        atm_strike,
        ",".join(str(s) for s in steps_otm_list),
        itm_ce_strike,
        itm_pe_strike,
    )

    return {
        "expiry_dt": nearest_expiry,
        "strike_step": strike_step,
        "atm_ce": atm_ce,
        "atm_pe": atm_pe,
        "otm_ce": otm_pairs[0]["ce"],
        "otm_pe": otm_pairs[0]["pe"],
        "otm_pairs": otm_pairs,
        "itm_ce": itm_ce,
        "itm_pe": itm_pe,
    }
