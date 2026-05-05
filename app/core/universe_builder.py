"""Instrument universe builder (part of §3 Broker Adapter Layer and §4 hub bootstrap)."""

# Build the instrument universe from YAML and broker scrip master data.
# Resolves tokens, exchange types, and option strikes for subscriptions.
import http.client
import json
import logging
import os
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple, Union

from .broker_network_identity import resolve_broker_network_identity
from .instruments_resolver import (
    get_front_future,
    load_scrip_master,
    find_index_underlying,
    pick_index_options_strikes,
)
from .rate_limiter import rate_limiter
from .strategy_config_loader import load_universe_from_yaml

# ExchangeType mapping used by SmartAPI
EX_NSE_CM = 1
EX_NSE_FO = 2
EX_BSE_CM = 3
EX_BSE_FO = 4
EX_MCX_FO = 5

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
DEFAULT_UNIVERSE_PATH = CONFIG_DIR / "universe.yaml"
_HISTORICAL_EXCHANGE_ALIASES = {
    "BFO": "BFO",
    "BSE": "BSE",
    "BSEFO": "BFO",
    "BSE_CM": "BSE",
    "INDICES": "NSE",
    "MCX": "MCX",
    "MCX_FO": "MCX",
    "NCO": "MCX",
    "NFO": "NFO",
    "NSE": "NSE",
    "NSEFO": "NFO",
    "NSE_CM": "NSE",
}


def _safe_float(raw: Any, default: float, minimum: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def _safe_int(raw: Any, default: int, minimum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


UNIVERSE_QUOTE_TIMEOUT_SECONDS = _safe_float(
    os.getenv("UNIVERSE_QUOTE_TIMEOUT_SECONDS", "8"),
    default=8.0,
    minimum=1.0,
)
UNIVERSE_QUOTE_MAX_ATTEMPTS = _safe_int(
    os.getenv("UNIVERSE_QUOTE_MAX_ATTEMPTS", "3"),
    default=3,
    minimum=1,
)
UNIVERSE_QUOTE_RETRY_BASE_SECONDS = _safe_float(
    os.getenv("UNIVERSE_QUOTE_RETRY_BASE_SECONDS", "0.5"),
    default=0.5,
    minimum=0.1,
)
UNIVERSE_QUOTE_RETRY_MAX_SECONDS = _safe_float(
    os.getenv("UNIVERSE_QUOTE_RETRY_MAX_SECONDS", "5"),
    default=5.0,
    minimum=0.1,
)


def _is_truthy_env(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _should_log_full_instrument_list() -> bool:
    override = os.getenv("UNIVERSE_LOG_FULL_LIST")
    if override is not None:
        return _is_truthy_env(override)
    trade_mode = str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper()
    return trade_mode != "LIVE"


def _exchange_type_for_segment(segment: str) -> int:
    seg = (segment or "").upper()
    if seg in ("NSE", "NSE_CM", "INDICES"):
        return EX_NSE_CM
    if seg in ("NFO", "NSEFO"):
        return EX_NSE_FO
    if seg in ("BSE", "BSE_CM"):
        return EX_BSE_CM
    if seg in ("BFO", "BSEFO", "BSE_FO"):
        return EX_BSE_FO
    if seg in ("MCX", "NCO"):
        return EX_MCX_FO
    return EX_NSE_CM


def normalize_historical_exchange(value: Any) -> str:
    exchange = str(value or "").strip().upper()
    return _HISTORICAL_EXCHANGE_ALIASES.get(exchange, exchange)


# Format strike values for labels with integer or 2-decimal precision.
def _format_strike(strike: float) -> str:
    return f"{strike:.0f}" if abs(strike - int(strike)) < 1e-6 else f"{strike:.2f}"


# Allow token overrides via environment variables.
def _override_token(label: str, default_token: Any, extra_env: Iterable[str] = ()):
    """
    Allow overriding tokens via environment variables.
    Checks both explicit names (extra_env) and TOKEN_<LABEL> / <LABEL>_TOKEN.
    """
    candidates = list(extra_env) + [f"TOKEN_{label}", f"{label}_TOKEN"]
    for env_name in candidates:
        override = os.getenv(env_name)
        if override:
            logger.info("Using override for %s: %s=%s", label, env_name, override)
            return str(override)
    return str(default_token)


# Return trading hours for the given underlying type.
def _get_trading_hours(underlying: str) -> Tuple[str, str]:
    """
    Determine trading hours (start_time, end_time) for an underlying.
    
    Args:
        underlying: Name of the underlying (e.g., "NG_FUT", "NIFTY_IDX", "BANKNIFTY_IDX")
    
    Returns:
        Tuple of (start_time, end_time) in HH:MM format
    """
    if "NG" in underlying and "FUT" in underlying:
        return ("09:00", "23:30")  # NG_FUT: Extended hours
    elif "_IDX" in underlying:
        return ("09:00", "15:30")  # All index underlyings: Standard market hours
    else:
        return ("09:00", "15:30")  # Default to market hours


# Build auth headers for Angel API calls.
def _build_secure_headers(jwt_token: str, api_key: str) -> Dict[str, str]:
    # TODO: legacy single-tenant env usage; route via Settings/broker adapter when hub routing is universal.
    network_identity = resolve_broker_network_identity(
        client_local_ip=os.getenv("CLIENT_LOCAL_IP"),
        client_public_ip=os.getenv("CLIENT_PUBLIC_IP"),
        mac_address=os.getenv("MAC_ADDRESS"),
        trade_mode=os.getenv("TRADE_MODE", "PAPER"),
        source="universe_builder",
    )
    return {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": network_identity.client_local_ip,
        "X-ClientPublicIP": network_identity.client_public_ip,
        "X-MACAddress": network_identity.mac_address,
        "X-PrivateKey": api_key,
    }


# Trim log text to a compact snippet.
def _truncate_log(text: str, limit: int = 512) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}...(truncated)"


# Fetch the last traded price for a token from Angel API.
def get_ltp_for_tokens(
    jwt_token: str,
    api_key: str,
    exchange_to_tokens: dict[str, list[str]],
) -> dict[tuple[str, str], float]:
    headers = _build_secure_headers(jwt_token, api_key)
    normalized: dict[str, list[str]] = {}
    requested_pairs: set[tuple[str, str]] = set()
    token_to_exchanges: dict[str, set[str]] = defaultdict(set)
    for exchange, raw_tokens in (exchange_to_tokens or {}).items():
        ex = str(exchange)
        tokens = [str(tok) for tok in (raw_tokens or []) if str(tok)]
        if not tokens:
            continue
        normalized[ex] = tokens
        for tok in tokens:
            requested_pairs.add((ex, tok))
            token_to_exchanges[tok].add(ex)
    if not normalized:
        return {}

    payload = json.dumps({"mode": "LTP", "exchangeTokens": normalized})
    request_summary = ", ".join(
        f"{ex}:{len(tokens)}" for ex, tokens in sorted(normalized.items())
    )
    last_error: Exception | None = None

    for attempt in range(1, UNIVERSE_QUOTE_MAX_ATTEMPTS + 1):
        proxy = (
            os.environ.get("ANGEL_HTTPS_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
        )
        if proxy:
            from urllib.parse import urlparse
            p = urlparse(proxy)
            conn = http.client.HTTPSConnection(
                p.hostname, p.port or 8080, timeout=UNIVERSE_QUOTE_TIMEOUT_SECONDS
            )
            conn.set_tunnel("apiconnect.angelone.in", 443)
        else:
            conn = http.client.HTTPSConnection(
                "apiconnect.angelone.in",
                timeout=UNIVERSE_QUOTE_TIMEOUT_SECONDS,
            )
        try:
            rate_limiter.acquire("quote")
            conn.request(
                "POST",
                "/rest/secure/angelbroking/market/v1/quote/",
                body=payload,
                headers=headers,
            )

            res = conn.getresponse()
            text = res.read().decode("utf-8", errors="replace")
            logger.debug("Quote raw response: %s", _truncate_log(text))

            if res.status >= 500:
                raise RuntimeError(
                    f"Quote request failed with status={res.status} body_head={_truncate_log(text, 256)}"
                )

            data = json.loads(text)
            if not data.get("status"):
                raise RuntimeError(f"Quote failed: {_truncate_log(text, 256)}")

            fetched = data.get("data", {}).get("fetched") or []
            if not fetched:
                raise RuntimeError("No LTP fetched")

            ltps: dict[tuple[str, str], float] = {}
            for item in fetched:
                if not isinstance(item, dict):
                    continue
                ltp = _safe_float(item.get("ltp"), default=-1.0, minimum=-1.0)
                if ltp <= 0:
                    continue
                token_val = str(
                    item.get("symbolToken")
                    or item.get("symboltoken")
                    or item.get("token")
                    or ""
                )
                if not token_val:
                    continue

                item_exchange = str(
                    item.get("exchange")
                    or item.get("exch_seg")
                    or ""
                )
                if item_exchange and (item_exchange, token_val) in requested_pairs:
                    ltps[(item_exchange, token_val)] = float(ltp)
                    continue

                exchanges = token_to_exchanges.get(token_val, set())
                if len(exchanges) == 1:
                    ex = next(iter(exchanges))
                    ltps[(ex, token_val)] = float(ltp)

            missing = [pair for pair in requested_pairs if pair not in ltps]
            if missing:
                raise RuntimeError(f"Missing LTP for {missing}")

            for (exchange, token), ltp in ltps.items():
                logger.debug("LTP: %s:%s = %.3f", exchange, token, ltp)
            return ltps
        except (
            TimeoutError,
            OSError,
            http.client.HTTPException,
            json.JSONDecodeError,
            RuntimeError,
            TypeError,
            KeyError,
            ValueError,
        ) as exc:
            last_error = exc
            if attempt >= UNIVERSE_QUOTE_MAX_ATTEMPTS:
                break

            backoff = min(
                UNIVERSE_QUOTE_RETRY_MAX_SECONDS,
                UNIVERSE_QUOTE_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
            )
            backoff *= 1.0 + random.uniform(0.0, 0.2)
            logger.warning(
                "Quote fetch failed for [%s] pairs=%d (attempt %d/%d): %s; retrying in %.2fs",
                request_summary,
                len(requested_pairs),
                attempt,
                UNIVERSE_QUOTE_MAX_ATTEMPTS,
                exc,
                backoff,
            )
            time.sleep(backoff)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    raise RuntimeError(
        f"Quote fetch failed after {UNIVERSE_QUOTE_MAX_ATTEMPTS} attempts"
    ) from last_error


# Fetch the last traded price for a token from Angel API.
def get_ltp_for_token(jwt_token: str, api_key: str, exchange: str, token: str) -> float:
    ltps = get_ltp_for_tokens(
        jwt_token=jwt_token,
        api_key=api_key,
        exchange_to_tokens={str(exchange): [str(token)]},
    )
    key = (str(exchange), str(token))
    if key not in ltps:
        raise RuntimeError(f"No LTP fetched for {exchange}:{token}")
    return float(ltps[key])


# Load the instrument universe YAML config from disk.
def _load_universe(universe_path: Union[str, Path]) -> Dict[str, Any]:
    data = load_universe_from_yaml(universe_path)
    underlyings: Any = data.get("underlyings")
    if not underlyings:
        raise ValueError(f"No underlyings defined in {universe_path}")
    return data


# Build instrument entries for a future underlying and its options.
def _build_future_underlying(
    entry: Dict[str, Any],
    df: Any,
    jwt_token: str,
    api_key: str,
) -> List[Dict[str, Any]]:
    name = entry.get("name", "").upper()
    symbol_pattern = entry.get("symbol_pattern", name)
    options_cfg = entry.get("options") or {}
    use_atm = options_cfg.get("use_atm", True)
    use_strangle_otm = options_cfg.get("use_strangle_otm", False)
    otm_steps = options_cfg.get("otm_steps", 1)
    itm_steps = int(options_cfg.get("itm_steps", 1))

    fut_meta = get_front_future(df)
    fut_token = _override_token(
        f"{name}_FUT",
        fut_meta["token"],
        extra_env=[f"{name}_FUT_TOKEN", f"{name}_MCX_TOKEN"],
    )
    fut_exch_type = _exchange_type_for_segment(fut_meta["exch_seg"])

    fut_ltp = get_ltp_for_token(jwt_token, api_key, fut_meta["exch_seg"], fut_token)

    start_time, end_time = _get_trading_hours(f"{name}_FUT")

    instruments: List[Dict[str, Any]] = [
        {
            "label": f"{name}_FUT",
            "underlying": name,
            "kind": "UNDERLYING",
            "exchangeType": fut_exch_type,
            "exchange": fut_meta["exch_seg"],
            "token": str(fut_token),
            "symbol": fut_meta["symbol"],
            "lot_size": entry.get("lot_size"),
            "tick_size": fut_meta.get("tick_size"),
            "start_time": start_time,
            "end_time": end_time,
        }
    ]

    try:
        opts = pick_index_options_strikes(
            df,
            base_symbol=symbol_pattern,
            index_ltp=fut_ltp,
            steps_otm=otm_steps,
            steps_itm=itm_steps,
            segments=("MCX", "NCO"),
            expiry_dt=fut_meta.get("expiry_dt"),
        )
    except Exception as e:
        logger.error("Failed to resolve options for %s: %s", name, e)
        return instruments

    lot_size = (
        entry.get("lot_size")
        or opts["atm_ce"].get("lot_size")
        or opts["atm_pe"].get("lot_size")
    )
    if lot_size is None:
        lot_size = 1

    option_exchange = (
        opts["atm_ce"].get("exch_seg", fut_meta["exch_seg"]) or fut_meta["exch_seg"]
    )
    option_exchange_type = _exchange_type_for_segment(option_exchange)

    if use_atm:
        instruments.extend(
            [
                {
                    "label": f"{name}_ATM_CE_{_format_strike(opts['atm_ce']['strike'])}",
                    "underlying": name,
                    "kind": "ATM_CE",
                    "exchangeType": option_exchange_type,
                    "exchange": option_exchange,
                    "token": opts["atm_ce"]["token"],
                    "symbol": opts["atm_ce"]["symbol"],
                    "lot_size": lot_size,
                    "tick_size": opts["atm_ce"].get("tick_size"),
                    "start_time": start_time,
                    "end_time": end_time,
                },
                {
                    "label": f"{name}_ATM_PE_{_format_strike(opts['atm_pe']['strike'])}",
                    "underlying": name,
                    "kind": "ATM_PE",
                    "exchangeType": option_exchange_type,
                    "exchange": option_exchange,
                    "token": opts["atm_pe"]["token"],
                    "symbol": opts["atm_pe"]["symbol"],
                    "lot_size": lot_size,
                    "tick_size": opts["atm_pe"].get("tick_size"),
                    "start_time": start_time,
                    "end_time": end_time,
                },
            ]
        )

    if use_strangle_otm:
        otm_pairs = opts.get("otm_pairs") or [{"ce": opts["otm_ce"], "pe": opts["otm_pe"]}]
        for pair in otm_pairs:
            ce = pair.get("ce")
            pe = pair.get("pe")
            if not ce or not pe:
                continue
            instruments.extend(
                [
                    {
                        "label": f"{name}_OTM_CE_{_format_strike(ce['strike'])}",
                        "underlying": name,
                        "kind": "OTM_CE",
                        "exchangeType": option_exchange_type,
                        "exchange": option_exchange,
                        "token": ce["token"],
                        "symbol": ce["symbol"],
                        "lot_size": lot_size,
                        "tick_size": ce.get("tick_size"),
                        "start_time": start_time,
                        "end_time": end_time,
                    },
                    {
                        "label": f"{name}_OTM_PE_{_format_strike(pe['strike'])}",
                        "underlying": name,
                        "kind": "OTM_PE",
                        "exchangeType": option_exchange_type,
                        "exchange": option_exchange,
                        "token": pe["token"],
                        "symbol": pe["symbol"],
                        "lot_size": lot_size,
                        "tick_size": pe.get("tick_size"),
                        "start_time": start_time,
                        "end_time": end_time,
                    },
                ]
            )

    return instruments


# Build instrument entries for an index underlying and its options.
def _build_index_underlying(
    entry: Dict[str, Any],
    df: Any,
    jwt_token: str,
    api_key: str,
) -> List[Dict[str, Any]]:
    name = entry.get("name", "").upper()
    symbol_pattern = entry.get("symbol_pattern", name)
    index_defaults = {
        "NIFTY": {"token": "99926000", "symbol": "NIFTY 50", "exch_seg": "NSE"},
        "BANKNIFTY": {"token": "99926009", "symbol": "NIFTY BANK", "exch_seg": "NSE"},
        "SENSEX": {"token": "99919000", "symbol": "SENSEX", "exch_seg": "BSE"},
        "FINNIFTY": {
            "token": "99926037",
            "symbol": "Nifty Fin Services",
            "exch_seg": "NSE",
        },
        "MIDCPNIFTY": {
            "token": "99926074",
            "symbol": "NIFTY MID SELECT",
            "exch_seg": "NSE",
        },
    }
    options_cfg = entry.get("options") or {}
    use_atm = options_cfg.get("use_atm", True)
    use_strangle_otm = options_cfg.get("use_strangle_otm", True)
    include_itm = options_cfg.get("use_itm", True)
    otm_steps = options_cfg.get("otm_steps", 1)
    itm_steps = int(options_cfg.get("itm_steps", 1))

    default_idx = index_defaults.get(name, {})
    idx_token_cfg = entry.get("index_token") or default_idx.get("token")
    idx_symbol_cfg = entry.get("index_symbol") or default_idx.get("symbol")
    idx_exch_cfg = entry.get("index_exchange") or default_idx.get("exch_seg")

    if idx_token_cfg and idx_symbol_cfg and idx_exch_cfg:
        idx_meta = {
            "token": str(idx_token_cfg),
            "symbol": str(idx_symbol_cfg),
            "exch_seg": str(idx_exch_cfg),
        }
    else:
        idx_meta = find_index_underlying(df, base_symbol=symbol_pattern)

    idx_token = _override_token(
        f"{name}_IDX", idx_meta["token"], extra_env=[f"{name}_IDX_TOKEN"]
    )
    idx_exchange_type = _exchange_type_for_segment(idx_meta["exch_seg"])

    idx_ltp = get_ltp_for_token(jwt_token, api_key, idx_meta["exch_seg"], idx_token)

    option_segments = options_cfg.get("option_segments")
    if not option_segments:
        option_segments = (
            ["BFO", "BSEFO"]
            if idx_meta.get("exch_seg", "").upper().startswith("BSE")
            else ["NFO", "NSEFO"]
        )

    opts = pick_index_options_strikes(
        df,
        base_symbol=symbol_pattern,
        index_ltp=idx_ltp,
        steps_otm=otm_steps,
        steps_itm=itm_steps,
        segments=option_segments,
    )

    lot_size = (
        entry.get("lot_size")
        or opts["atm_ce"].get("lot_size")
        or opts["atm_pe"].get("lot_size")
        or 1
    )

    option_exchange = opts["atm_ce"].get("exch_seg", "NFO") or "NFO"
    option_exchange_type = _exchange_type_for_segment(option_exchange)

    start_time, end_time = _get_trading_hours(f"{name}_IDX")

    instruments: List[Dict[str, Any]] = [
        {
            "label": f"{name}_IDX",
            "underlying": name,
            "kind": "UNDERLYING",
            "exchangeType": idx_exchange_type,
            "exchange": idx_meta["exch_seg"],
            "token": str(idx_token),
            "symbol": idx_meta["symbol"],
            "lot_size": lot_size,
            "tick_size": idx_meta.get("tick_size"),
            "start_time": start_time,
            "end_time": end_time,
        }
    ]

    if use_atm:
        instruments.extend(
            [
                {
                    "label": f"{name}_ATM_CE_{_format_strike(opts['atm_ce']['strike'])}",
                    "underlying": name,
                    "kind": "ATM_CE",
                    "exchangeType": option_exchange_type,
                    "exchange": option_exchange,
                    "token": opts["atm_ce"]["token"],
                    "symbol": opts["atm_ce"]["symbol"],
                    "lot_size": lot_size,
                    "tick_size": opts["atm_ce"].get("tick_size"),
                    "start_time": start_time,
                    "end_time": end_time,
                },
                {
                    "label": f"{name}_ATM_PE_{_format_strike(opts['atm_pe']['strike'])}",
                    "underlying": name,
                    "kind": "ATM_PE",
                    "exchangeType": option_exchange_type,
                    "exchange": option_exchange,
                    "token": opts["atm_pe"]["token"],
                    "symbol": opts["atm_pe"]["symbol"],
                    "lot_size": lot_size,
                    "tick_size": opts["atm_pe"].get("tick_size"),
                    "start_time": start_time,
                    "end_time": end_time,
                },
            ]
        )

    if use_strangle_otm:
        otm_pairs = opts.get("otm_pairs") or [{"ce": opts["otm_ce"], "pe": opts["otm_pe"]}]
        for pair in otm_pairs:
            ce = pair.get("ce")
            pe = pair.get("pe")
            if not ce or not pe:
                continue
            instruments.extend(
                [
                    {
                        "label": f"{name}_OTM_CE_{_format_strike(ce['strike'])}",
                        "underlying": name,
                        "kind": "OTM_CE",
                        "exchangeType": option_exchange_type,
                        "exchange": option_exchange,
                        "token": ce["token"],
                        "symbol": ce["symbol"],
                        "lot_size": lot_size,
                        "tick_size": ce.get("tick_size"),
                        "start_time": start_time,
                        "end_time": end_time,
                    },
                    {
                        "label": f"{name}_OTM_PE_{_format_strike(pe['strike'])}",
                        "underlying": name,
                        "kind": "OTM_PE",
                        "exchangeType": option_exchange_type,
                        "exchange": option_exchange,
                        "token": pe["token"],
                        "symbol": pe["symbol"],
                        "lot_size": lot_size,
                        "tick_size": pe.get("tick_size"),
                        "start_time": start_time,
                        "end_time": end_time,
                    },
                ]
            )

    if include_itm:
        instruments.extend(
            [
                {
                    "label": f"{name}_ITM_CE_{_format_strike(opts['itm_ce']['strike'])}",
                    "underlying": name,
                    "kind": "ITM_CE",
                    "exchangeType": option_exchange_type,
                    "exchange": option_exchange,
                    "token": opts["itm_ce"]["token"],
                    "symbol": opts["itm_ce"]["symbol"],
                    "lot_size": lot_size,
                    "tick_size": opts["itm_ce"].get("tick_size"),
                    "start_time": start_time,
                    "end_time": end_time,
                },
                {
                    "label": f"{name}_ITM_PE_{_format_strike(opts['itm_pe']['strike'])}",
                    "underlying": name,
                    "kind": "ITM_PE",
                    "exchangeType": option_exchange_type,
                    "exchange": option_exchange,
                    "token": opts["itm_pe"]["token"],
                    "symbol": opts["itm_pe"]["symbol"],
                    "lot_size": lot_size,
                    "tick_size": opts["itm_pe"].get("tick_size"),
                    "start_time": start_time,
                    "end_time": end_time,
                },
            ]
        )

    return instruments


# Build a data-only instrument (no options) for monitoring feeds like India VIX.
def _build_data_underlying(
    entry: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Subscribe to a single index tick without resolving option chains.
    Used for non-tradeable data feeds (e.g. India VIX for circuit breaker).
    """
    name = entry.get("name", "").upper()
    idx_token = str(entry.get("index_token", ""))
    idx_symbol = str(entry.get("index_symbol", ""))
    idx_exchange = str(entry.get("index_exchange", "NSE"))
    lot_size = entry.get("lot_size", 1)

    idx_token = _override_token(
        f"{name}_IDX", idx_token, extra_env=[f"{name}_IDX_TOKEN"]
    )
    exchange_type = _exchange_type_for_segment(idx_exchange)
    start_time, end_time = _get_trading_hours(f"{name}_IDX")

    logger.info(
        "   DATA underlying %s: token=%s symbol=%s exchange=%s",
        name, idx_token, idx_symbol, idx_exchange,
    )
    return [
        {
            "label": f"{name}_IDX",
            "underlying": name,
            "kind": "DATA",
            "exchangeType": exchange_type,
            "exchange": idx_exchange,
            "token": str(idx_token),
            "symbol": idx_symbol,
            "lot_size": lot_size,
            "tick_size": None,
            "start_time": start_time,
            "end_time": end_time,
        }
    ]


# Build the full universe, token labels, and token list for subscriptions.
def build_instrument_universe(
    jwt_token: str,
    api_key: str,
    universe_path: Union[str, Path] = DEFAULT_UNIVERSE_PATH,
) -> Tuple[
    List[Dict[str, Any]],
    Dict[str, str],
    List[Dict[str, Any]],
    Dict[str, Dict[str, Any]],
]:
    """
    Builds the instrument universe from universe.yaml.
    Returns (instruments, token_labels, token_list, instrument_meta).
    """
    logger.info("[BUILD] [UNIVERSE] Starting build_instrument_universe")
    logger.info("   Universe path: %s", universe_path)

    cfg = _load_universe(universe_path)
    logger.info("[OK] [UNIVERSE] Loaded config with %d underlyings", len(cfg.get("underlyings", [])))

    df = load_scrip_master()
    logger.info("[OK] [UNIVERSE] Loaded scrip master with %d rows", len(df))

    instruments: List[Dict[str, Any]] = []
    for entry in cfg.get("underlyings", []):
        enabled = entry.get("enabled", True)
        typ = (entry.get("type") or "").upper()
        name = entry.get("name")

        if not enabled:
            logger.info("[SKIP] [UNIVERSE] Skipping disabled: %s", name)
            continue

        if not name:
            logger.warning("[SKIP] [UNIVERSE] Skipping entry without name: %s", entry)
            continue

        logger.info("[BUILDING] [UNIVERSE] Building %s (%s)", name, typ)
        try:
            if typ == "FUT":
                new_instruments = _build_future_underlying(entry, df, jwt_token, api_key)
                logger.info("   [OK] Added %d future instruments for %s", len(new_instruments), name)
                instruments.extend(new_instruments)
            elif typ == "INDEX":
                new_instruments = _build_index_underlying(entry, df, jwt_token, api_key)
                logger.info("   [OK] Added %d index instruments for %s", len(new_instruments), name)
                instruments.extend(new_instruments)
            elif typ == "DATA":
                new_instruments = _build_data_underlying(entry)
                logger.info("   [OK] Added %d data instruments for %s", len(new_instruments), name)
                instruments.extend(new_instruments)
            else:
                logger.warning("[SKIP] [UNIVERSE] Unsupported type %s for %s, skipping", typ, name)
        except Exception as e:
            err_str = str(e)
            if (
                "AG8001" in err_str
                or "invalid token" in err_str.lower()
                or "token expired" in err_str.lower()
                or "unauthorized" in err_str.lower()
            ):
                raise  # propagate auth errors so the caller can re-login
            logger.error("[ERROR] [UNIVERSE] Error building %s: %s", name, e, exc_info=True)

    logger.info("[STATS] [UNIVERSE] Total instruments built: %d", len(instruments))

    token_labels = {str(inst["token"]): inst["label"] for inst in instruments}
    logger.info("[LABELS] [UNIVERSE] Created %d token labels", len(token_labels))

    tokens_by_exchange = defaultdict(list)
    for inst in instruments:
        tokens_by_exchange[inst["exchangeType"]].append(str(inst["token"]))

    token_list = [
        {"exchangeType": ex_type, "tokens": tok_list}
        for ex_type, tok_list in tokens_by_exchange.items()
    ]

    logger.info("[EXCHANGES] [UNIVERSE] Instruments by exchange:")
    for ex_type, tokens in tokens_by_exchange.items():
        logger.info("   Exchange %s: %d tokens", ex_type, len(tokens))

    instrument_meta = {inst["label"]: inst for inst in instruments}

    if not _should_log_full_instrument_list():
        logger.info("[LIST] [UNIVERSE] Full instrument list suppressed; set UNIVERSE_LOG_FULL_LIST=1 to enable.")
        logger.info("[OK] [UNIVERSE] build_instrument_universe COMPLETE")
        return instruments, token_labels, token_list, instrument_meta

    logger.info("[LIST] [UNIVERSE] Full instrument list:")
    for inst in instruments:
        logger.info(
            "   [OK] %s | token=%s | ex=%s | segment=%s",
            inst["label"],
            inst["token"],
            inst["exchangeType"],
            inst.get("segment", "N/A"),
        )

    logger.info("[OK] [UNIVERSE] build_instrument_universe COMPLETE")
    return instruments, token_labels, token_list, instrument_meta
