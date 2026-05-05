"""Implements parts of §3 Broker Adapter Layer (Angel order client, legacy single-tenant)."""

# order_client.py
# Thin HTTP client for placing orders via Angel One SmartAPI.
# Uses the same JWT + API_KEY returned by angel_login_and_get_tokens.
# Handles block detection, response parsing, and retries.

from __future__ import annotations

import http.client
import json
import os
import logging
import time
import random
import math
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple

from app.core.broker_network_identity import resolve_broker_network_identity
from app.core.rate_limiter import rate_limiter

logger = logging.getLogger("order_client")

API_HOST = "apiconnect.angelone.in"
HTTP_TIMEOUT_SECONDS = float(os.getenv("ANGEL_HTTP_TIMEOUT_SECONDS", "10"))


def _make_angel_connection() -> http.client.HTTPSConnection:
    proxy = (
        os.environ.get("ANGEL_HTTPS_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
    )
    if proxy:
        from urllib.parse import urlparse
        p = urlparse(proxy)
        conn = http.client.HTTPSConnection(
            p.hostname, p.port or 8080, timeout=HTTP_TIMEOUT_SECONDS
        )
        conn.set_tunnel(API_HOST, 443)
        return conn
    return http.client.HTTPSConnection(API_HOST, timeout=HTTP_TIMEOUT_SECONDS)
HISTORICAL_TIMEZONE = timezone(timedelta(hours=5, minutes=30))

class AngelHTTPBlockedError(RuntimeError):
    # Build a blocked-response error with request context.
    def __init__(
        self,
        message: str,
        *,
        url: str,
        status: Optional[int] = None,
        body_head: Optional[str] = None,
        content_type: Optional[str] = None,
        retry_after_seconds: Optional[float] = None,
    ) -> None:
        parts = [message, f"url={url}"]
        if status is not None:
            parts.append(f"status={status}")
        if content_type:
            parts.append(f"content_type={content_type}")
        if retry_after_seconds is not None:
            parts.append(f"retry_after={retry_after_seconds}")
        if body_head:
            parts.append(f"head={body_head}")
        super().__init__(" ".join(parts))
        self.url = url
        self.status = status
        self.body_head = body_head
        self.content_type = content_type
        self.retry_after_seconds = retry_after_seconds


class AngelHTTPResponseError(RuntimeError):
    # Build a response error with HTTP status and body details.
    def __init__(
        self,
        message: str,
        *,
        url: str,
        status: Optional[int] = None,
        body_head: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> None:
        parts = [message, f"url={url}"]
        if status is not None:
            parts.append(f"status={status}")
        if content_type:
            parts.append(f"content_type={content_type}")
        if body_head:
            parts.append(f"head={body_head}")
        super().__init__(" ".join(parts))
        self.url = url
        self.status = status
        self.body_head = body_head
        self.content_type = content_type


# Return a compact head of a response body for logging.
def _response_head(text: str, limit: int = 120) -> str:
    compact = " ".join(text.strip().split())
    return compact[:limit]


# Trim a long string for safe logging.
def _truncate(val: str, n: int = 512) -> str:
    compact = " ".join(val.strip().split())
    if len(compact) <= n:
        return compact
    return f"{compact[:n]}...(truncated)"


def _normalize_tick_size(raw_tick_size: Optional[float]) -> Optional[float]:
    try:
        tick_size = float(raw_tick_size)
    except (TypeError, ValueError):
        return None
    if tick_size <= 0:
        return None
    if tick_size >= 1.0:
        tick_size /= 100.0
    return tick_size


def _price_precision(tick_size: Optional[float]) -> int:
    normalized = _normalize_tick_size(tick_size)
    if normalized is None:
        return 2
    text = f"{normalized:.8f}".rstrip("0").rstrip(".")
    if "." not in text:
        return 0
    return max(2, len(text.split(".", 1)[1]))


def _snap_price_to_tick(
    price: Optional[float],
    *,
    tick_size: Optional[float],
    transaction_type: str,
) -> Optional[float]:
    if price is None:
        return None
    try:
        raw_price = float(price)
    except (TypeError, ValueError):
        return None
    normalized_tick = _normalize_tick_size(tick_size)
    if normalized_tick is None or raw_price <= 0:
        return raw_price
    units = raw_price / normalized_tick
    side = str(transaction_type or "").upper()
    if side == "BUY":
        snapped = math.floor(units + 1e-9) * normalized_tick
    elif side == "SELL":
        snapped = math.ceil(units - 1e-9) * normalized_tick
    else:
        snapped = round(units) * normalized_tick
    if snapped <= 0:
        snapped = normalized_tick
    precision = _price_precision(normalized_tick)
    return round(snapped, precision)


def _format_price_value(price: Optional[float], *, tick_size: Optional[float]) -> str:
    if price is None:
        return "0"
    precision = _price_precision(tick_size)
    return f"{float(price):.{precision}f}"


# Detect HTML or WAF-style responses instead of JSON.
def _is_html_like(text: str) -> bool:
    lowered = text.strip().lower()
    markers = ("<html", "<!doctype", "request rejected", "go back", "<body", "</html")
    return any(marker in lowered for marker in markers)


# Parse Retry-After header into seconds if present.
def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _default_rate_limit_retry_after_seconds() -> float:
    raw = os.getenv("ANGEL_RATE_LIMIT_RETRY_AFTER_SECONDS", "15")
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 15.0


def _format_historical_datetime(value: datetime) -> str:
    ts = value
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts.astimezone(HISTORICAL_TIMEZONE).strftime("%Y-%m-%d %H:%M")


# Read an HTTP response, validate JSON, and raise on blocked responses.
def _read_json_response(
    res: http.client.HTTPResponse, *, url: str
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    text = res.read().decode("utf-8", errors="replace")
    headers = {k.lower(): v for k, v in res.getheaders()}
    content_type = headers.get("content-type", "")
    retry_after = _parse_retry_after(headers.get("retry-after"))
    blocked_retry_after = retry_after
    if blocked_retry_after is None and res.status in {403, 429}:
        blocked_retry_after = _default_rate_limit_retry_after_seconds()
    if not text.strip():
        raise AngelHTTPBlockedError(
            "Empty response",
            url=url,
            status=res.status,
            content_type=content_type,
            retry_after_seconds=blocked_retry_after,
        )
    if res.status == 403 and "exceeding access rate" in text.lower():
        raise AngelHTTPBlockedError(
            "Rate limit exceeded",
            url=url,
            status=res.status,
            content_type=content_type,
            body_head=_response_head(text),
            retry_after_seconds=blocked_retry_after,
        )
    if _is_html_like(text):
        raise AngelHTTPBlockedError(
            "HTML response",
            url=url,
            status=res.status,
            content_type=content_type,
            body_head=_response_head(text),
            retry_after_seconds=blocked_retry_after,
        )
    if "application/json" not in content_type.lower():
        raise AngelHTTPBlockedError(
            "Non-JSON response",
            url=url,
            status=res.status,
            content_type=content_type,
            body_head=_response_head(text),
            retry_after_seconds=blocked_retry_after,
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AngelHTTPResponseError(
            "JSON decode failed",
            url=url,
            status=res.status,
            content_type=content_type,
            body_head=_response_head(text),
        ) from exc
    return text, headers, data


_REDACT_KEYS = {
    "orderid",
    "order_id",
    "jwt",
    "jwttoken",
    "refreshtoken",
    "feedtoken",
}


# Redact sensitive fields from payloads before logging.
def _redact_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        redacted: Dict[str, Any] = {}
        for key, value in payload.items():
            key_lower = str(key).lower()
            if key_lower in _REDACT_KEYS:
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = _redact_payload(value)
        return redacted
    if isinstance(payload, list):
        return [_redact_payload(item) for item in payload]
    return payload


# Return a compact, redacted payload string for logs.
def _payload_head(payload: Any) -> str:
    if isinstance(payload, (dict, list)):
        try:
            text = json.dumps(_redact_payload(payload), ensure_ascii=False)
        except Exception:
            text = str(payload)
    else:
        text = str(payload)
    return _truncate(text, 512)


# Extract a request or correlation id from response headers.
def _extract_request_id(headers: Dict[str, str]) -> str:
    for key in ("x-request-id", "x-correlation-id", "request-id", "correlation-id"):
        val = headers.get(key)
        if val:
            return val
    return ""


class AngelOrderClient:
    # Initialize the client with JWT and API key.
    def __init__(
        self,
        jwt_token: str,
        api_key: str,
        *,
        broker_account_id: Optional[str] = None,
        client_local_ip: Optional[str] = None,
        client_public_ip: Optional[str] = None,
        mac_address: Optional[str] = None,
    ) -> None:
        self.jwt_token = jwt_token
        self.api_key = api_key
        self.broker_account_id = str(broker_account_id or "").strip() or None
        network_identity = resolve_broker_network_identity(
            client_local_ip=client_local_ip or os.getenv("CLIENT_LOCAL_IP"),
            client_public_ip=client_public_ip or os.getenv("CLIENT_PUBLIC_IP"),
            mac_address=mac_address or os.getenv("MAC_ADDRESS"),
            trade_mode=os.getenv("TRADE_MODE", "PAPER"),
            source=f"AngelOrderClient[{self.broker_account_id or 'default'}]",
        )
        self.client_local_ip = network_identity.client_local_ip
        self.client_public_ip = network_identity.client_public_ip
        self.mac_address = network_identity.mac_address

    # Build SmartAPI request headers for the current session.
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.jwt_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": self.client_local_ip,
            "X-ClientPublicIP": self.client_public_ip,
            "X-MACAddress": self.mac_address,
            "X-PrivateKey": self.api_key,
        }

    # Fetch positions from a specific endpoint and log response metadata.
    def _fetch_positions_endpoint(
        self,
        *,
        method: str,
        path: str,
        url: str,
        body: Optional[str] = None,
        limiter_key: str = "getAllPositions",
    ) -> Dict[str, Any]:
        rate_limiter.acquire(limiter_key)
        conn = _make_angel_connection()
        if body is None:
            conn.request(method, path, headers=self._headers())
        else:
            conn.request(method, path, body=body, headers=self._headers())
        res = conn.getresponse()
        _text, headers, data = _read_json_response(res, url=url)
        request_id = _extract_request_id(headers)
        logger.info(
            "Positions response status=%s event=POSITIONS_RESPONSE request_id=%s endpoint=%s",
            res.status,
            request_id or "-",
            path,
        )
        logger.debug("Positions response head=%r", _payload_head(data))
        return data

    # Place a broker order via SmartAPI.
    def place_order(
        self,
        *,
        exchange: str,
        tradingsymbol: str,
        symboltoken: str,
        transaction_type: str,  # "BUY" / "SELL"
        quantity: int,
        producttype: str = "INTRADAY",
        ordertype: str = "MARKET",
        variety: str = "NORMAL",
        duration: str = "DAY",
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        squareoff: float = 0.0,
        stoploss: float = 0.0,
        tag: Optional[str] = None,
        tick_size: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Basic placeOrder wrapper for options/futures.
        """
        order_type = str(ordertype or "").upper()
        side = str(transaction_type or "").upper()
        normalized_price = _snap_price_to_tick(
            price,
            tick_size=tick_size,
            transaction_type=side,
        )
        normalized_trigger_price = _snap_price_to_tick(
            trigger_price,
            tick_size=tick_size,
            transaction_type=side,
        )

        if order_type in {"MARKET", "SLM"}:
            price_str = "0"
        else:
            price_str = _format_price_value(normalized_price, tick_size=tick_size)

        payload = {
            "variety": variety,
            "tradingsymbol": tradingsymbol,
            "symboltoken": str(symboltoken),
            "transactiontype": side,  # BUY / SELL
            "exchange": exchange,
            "ordertype": order_type,  # MARKET / LIMIT / SL / SLM
            "producttype": producttype,
            "duration": duration,  # DAY / IOC
            "price": price_str,
            "squareoff": str(squareoff),
            "stoploss": str(stoploss),
            "quantity": str(quantity),
        }

        if normalized_trigger_price is not None:
            payload["triggerprice"] = _format_price_value(
                normalized_trigger_price,
                tick_size=tick_size,
            )
        if tag:
            tag_str = str(tag)
            if len(tag_str) > 20:
                logger.warning(
                    "ordertag too long (%d); truncating to 20 chars: %s",
                    len(tag_str),
                    tag_str,
                )
                tag_str = tag_str[:20]
            payload["ordertag"] = tag_str

        body = json.dumps(payload)
        logger.debug("Placing order payload_head=%s", _payload_head(payload))

        conn = _make_angel_connection()
        rate_limiter.acquire("placeOrder")
        conn.request(
            "POST",
            "/rest/secure/angelbroking/order/v1/placeOrder",
            body=body,
            headers=self._headers(),
        )
        res = conn.getresponse()
        _text, headers, data = _read_json_response(
            res,
            url="https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/placeOrder",
        )
        request_id = _extract_request_id(headers)
        logger.info(
            "Order response status=%s event=ORDER_RESPONSE request_id=%s",
            res.status,
            request_id or "-",
        )
        logger.debug("Order response head=%r", _payload_head(data))
        if not data.get("status"):
            raise RuntimeError(f"Order failed: {data}")

        return data

    # Fetch RMS information for the broker account.
    def get_rms(self) -> Dict[str, Any]:
        """
        Fetch RMS details; useful before trading in LIVE mode.
        """
        rate_limiter.acquire("getRMS")
        conn = _make_angel_connection()
        conn.request(
            "GET",
            "/rest/secure/angelbroking/user/v1/getRMS",
            headers=self._headers(),
        )
        res = conn.getresponse()
        _text, _headers, data = _read_json_response(
            res, url="https://apiconnect.angelone.in/rest/secure/angelbroking/user/v1/getRMS"
        )
        request_id = _extract_request_id(_headers)
        logger.info(
            "RMS response status=%s event=RMS_RESPONSE request_id=%s",
            res.status,
            request_id or "-",
        )
        logger.debug("RMS response head=%r", _payload_head(data))
        if not data.get("status"):
            raise RuntimeError(f"getRMS failed: {data}")
        return data

    # Fetch historical candles for a token and interval.
    def fetch_historical_candles(
        self,
        *,
        exchange: str,
        symboltoken: str,
        interval: str,
        from_datetime: datetime,
        to_datetime: datetime,
    ) -> list[list[Any]]:
        payload = {
            "exchange": str(exchange),
            "symboltoken": str(symboltoken),
            "interval": str(interval),
            "fromdate": _format_historical_datetime(from_datetime),
            "todate": _format_historical_datetime(to_datetime),
        }
        body = json.dumps(payload)
        logger.debug("Historical candle request payload_head=%s", _payload_head(payload))
        rate_limiter.acquire("historical")
        conn = _make_angel_connection()
        conn.request(
            "POST",
            "/rest/secure/angelbroking/historical/v1/getCandleData",
            body=body,
            headers=self._headers(),
        )
        res = conn.getresponse()
        _text, headers, data = _read_json_response(
            res,
            url="https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData",
        )
        request_id = _extract_request_id(headers)
        logger.info(
            "Historical response status=%s event=HISTORICAL_RESPONSE request_id=%s exchange=%s interval=%s",
            res.status,
            request_id or "-",
            exchange,
            interval,
        )
        logger.debug("Historical response head=%r", _payload_head(data))
        if not data.get("status"):
            raise RuntimeError(f"getCandleData failed: {data}")
        data_block = data.get("data", {})
        candles = data_block.get("candles") if isinstance(data_block, dict) else data_block
        if not isinstance(candles, list):
            return []
        return list(candles)

    # Fetch live positions with blocking detection and fallback logic.
    def get_positions(self) -> Dict[str, Any]:
        """Fetch live positions from Angel One SmartAPI.

        Key improvement vs previous version
        ---------------------------------
        If the primary positions endpoint returns a *blocked* response (403 rate-limit,
        HTML/WAF, empty body, non-JSON), we **do not** immediately hit the fallback endpoint.
        Doing so doubles the request burst and often worsens the block.

        Rules
        -----
        1) BLOCKED on primary  -> raise immediately (caller should back off)
        2) Non-blocked error   -> try fallback endpoint once
        3) Retry whole flow once (small jitter) for transient non-blocked failures
        """
        last_err: Optional[Exception] = None

        prefer = os.getenv("ANGEL_POSITIONS_ENDPOINT", "order").strip().lower()

        order_endpoint = (
            "GET",
            "/rest/secure/angelbroking/order/v1/getPosition",
            "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/getPosition",
            None,
            "getPosition",
        )
        portfolio_endpoint = (
            "POST",
            "/rest/secure/angelbroking/portfolio/v1/getAllPositions",
            "https://apiconnect.angelone.in/rest/secure/angelbroking/portfolio/v1/getAllPositions",
            "{}",
            "getAllPositions",
        )

        if prefer.startswith("port"):
            endpoints = [portfolio_endpoint, order_endpoint]
        else:
            endpoints = [order_endpoint, portfolio_endpoint]

        for attempt in range(2):
            primary = endpoints[0]
            fallback = endpoints[1]

            # ---- Primary ----
            try:
                data = self._fetch_positions_endpoint(
                    method=primary[0],
                    path=primary[1],
                    url=primary[2],
                    body=primary[3],
                    limiter_key=primary[4],
                )
                if data.get("status"):
                    return data

                # JSON returned but status=false
                last_err = AngelHTTPResponseError(
                    "Positions endpoint returned status=false",
                    url=primary[2],
                    status=None,
                    content_type="application/json",
                    body_head=_payload_head(data),
                )
                logger.warning(
                    "Positions endpoint status=false; trying fallback endpoint=%s",
                    fallback[1],
                )
            except AngelHTTPBlockedError as exc:
                # IMPORTANT: do not call fallback when blocked.
                last_err = exc
                logger.warning(
                    "Positions endpoint blocked; skipping fallback to avoid request storm: endpoint=%s status=%s content_type=%s retry_after=%s",
                    primary[1],
                    exc.status,
                    exc.content_type,
                    exc.retry_after_seconds,
                )
                if exc.body_head:
                    logger.debug(
                        "Positions blocked response head=%s",
                        _truncate(exc.body_head, 512),
                    )
                raise
            except AngelHTTPResponseError as exc:
                last_err = exc
                logger.warning(
                    "Positions endpoint error; trying fallback: endpoint=%s status=%s content_type=%s",
                    fallback[1],
                    exc.status,
                    exc.content_type,
                )
                if exc.body_head:
                    logger.debug(
                        "Positions error response head=%s",
                        _truncate(exc.body_head, 512),
                    )

            # ---- Fallback (only if primary was NOT blocked) ----
            try:
                data = self._fetch_positions_endpoint(
                    method=fallback[0],
                    path=fallback[1],
                    url=fallback[2],
                    body=fallback[3],
                    limiter_key=fallback[4],
                )
                if data.get("status"):
                    return data
                last_err = AngelHTTPResponseError(
                    "Fallback positions endpoint returned status=false",
                    url=fallback[2],
                    status=None,
                    content_type="application/json",
                    body_head=_payload_head(data),
                )
            except AngelHTTPBlockedError as exc:
                last_err = exc
                logger.warning(
                    "Fallback positions endpoint blocked: endpoint=%s status=%s content_type=%s retry_after=%s",
                    fallback[1],
                    exc.status,
                    exc.content_type,
                    exc.retry_after_seconds,
                )
                if exc.body_head:
                    logger.debug(
                        "Fallback blocked response head=%s",
                        _truncate(exc.body_head, 512),
                    )
                raise
            except AngelHTTPResponseError as exc:
                last_err = exc

            # ---- Retry once for non-blocked failures ----
            if attempt == 0:
                backoff = 0.5 + random.uniform(0.0, 0.25)
                logger.warning(
                    "get_positions attempt %d failed (%s); retrying in %.2fs",
                    attempt + 1,
                    last_err,
                    backoff,
                )
                time.sleep(backoff)
                continue

            break

        raise RuntimeError(f"get_positions failed after retry: {last_err}")



    # Fetch the broker order book.
    def get_order_book(self) -> Dict[str, Any]:
        """
        Fetch order book (all orders) from Angel One SmartAPI.
        """
        rate_limiter.acquire("getOrderBook")
        conn = _make_angel_connection()
        conn.request(
            "GET",
            "/rest/secure/angelbroking/order/v1/getOrderBook",
            headers=self._headers(),
        )
        res = conn.getresponse()
        _text, _headers, data = _read_json_response(
            res,
            url="https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/getOrderBook",
        )
        request_id = _extract_request_id(_headers)
        logger.info(
            "Order book response status=%s event=ORDER_BOOK_RESPONSE request_id=%s",
            res.status,
            request_id or "-",
        )
        logger.debug("Order book response head=%r", _payload_head(data))
        if not data.get("status"):
            raise RuntimeError(f"getOrderBook failed: {data}")
        return data

    # Cancel an existing broker order by order id.
    def cancel_order(
        self,
        order_id: str,
        *,
        variety: str = "NORMAL",
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not order_id:
            raise ValueError("order_id is required")
        payload = {
            "variety": str(variety or "NORMAL").upper(),
            "orderid": str(order_id),
        }
        body = json.dumps(payload)
        logger.debug(
            "Cancelling order payload_head=%s symbol=%s",
            _payload_head(payload),
            symbol or "-",
        )
        rate_limiter.acquire("cancelOrder")
        conn = _make_angel_connection()
        conn.request(
            "POST",
            "/rest/secure/angelbroking/order/v1/cancelOrder",
            body=body,
            headers=self._headers(),
        )
        res = conn.getresponse()
        _text, headers, data = _read_json_response(
            res,
            url="https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/cancelOrder",
        )
        request_id = _extract_request_id(headers)
        logger.info(
            "Cancel order response status=%s event=CANCEL_ORDER_RESPONSE request_id=%s order_id=%s",
            res.status,
            request_id or "-",
            order_id,
        )
        logger.debug("Cancel order response head=%r", _payload_head(data))
        return data

    # Fetch a single order's details.
    def get_order_details(self, order_id: str) -> Dict[str, Any]:
        """Fetch order details for a single order id."""
        if not order_id:
            raise ValueError("order_id is required")
        rate_limiter.acquire("getOrderDetails")
        conn = _make_angel_connection()
        conn.request(
            "GET",
            f"/rest/secure/angelbroking/order/v1/details/{order_id}",
            headers=self._headers(),
        )
        res = conn.getresponse()
        _text, _headers, data = _read_json_response(
            res,
            url=f"https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/details/{order_id}",
        )
        request_id = _extract_request_id(_headers)
        logger.info(
            "Order details response status=%s event=ORDER_DETAILS_RESPONSE request_id=%s order_id=%s",
            res.status,
            request_id or "-",
            order_id,
        )
        logger.debug("Order details response head=%r", _payload_head(data))
        if not data.get("status"):
            raise RuntimeError(f"getOrderDetails failed: {data}")
        return data

    # ----------------------
    # Order confirmation helpers
    # ----------------------

    _TERMINAL_ORDER_STATUSES = {"COMPLETE", "REJECTED", "CANCELLED", "EXPIRED"}

    # Normalize inconsistent broker order status values.
    @staticmethod
    def _normalize_order_status(value: Any) -> str:
        """Normalize SmartAPI order status text into a stable token.

        Angel orderbook statuses are not perfectly consistent across endpoints and
        versions (e.g., "complete", "COMPLETE", "trigger pending").
        """
        raw = " ".join(str(value or "").strip().split()).lower()
        if not raw:
            return "UNKNOWN"
        if raw in ("complete", "completed", "filled", "fill"):
            return "COMPLETE"
        if raw in ("rejected", "reject"):
            return "REJECTED"
        if raw in ("cancelled", "canceled", "cancel"):
            return "CANCELLED"
        if raw in ("expired",):
            return "EXPIRED"
        if "trigger" in raw and "pending" in raw:
            return "PENDING"
        if raw in ("pending", "open", "put order req received", "validation pending"):
            return "PENDING"
        return raw.upper()

    # Extract order id from a broker response payload.
    @staticmethod
    def _extract_order_id(resp: Dict[str, Any]) -> str:
        if not isinstance(resp, dict):
            return ""
        for key in ("orderid", "order_id"):
            val = resp.get(key)
            if val:
                return str(val)
        data = resp.get("data")
        if isinstance(data, dict):
            for key in ("orderid", "order_id"):
                val = data.get(key)
                if val:
                    return str(val)
        return ""

    # Find a specific order row inside the order book payload.
    @staticmethod
    def _find_order_row(order_book: Dict[str, Any], order_id: str) -> Optional[Dict[str, Any]]:
        if not order_id:
            return None
        if not isinstance(order_book, dict):
            return None
        rows = order_book.get("data")
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            rid = row.get("orderid") or row.get("order_id") or row.get("orderId")
            if rid and str(rid) == str(order_id):
                return row
        return None

    # Poll the order book until the order reaches a terminal state.
    def confirm_order_terminal(
        self,
        order_id: str,
        *,
        timeout_seconds: float = 8.0,
        poll_seconds: float = 0.8,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Poll order book until the order reaches a terminal state.

        Returns: (terminal_status, order_row)
        terminal_status is one of: COMPLETE / REJECTED / CANCELLED / EXPIRED / PENDING / UNKNOWN
        """
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        last_status = "UNKNOWN"
        last_row: Optional[Dict[str, Any]] = None
        while True:
            try:
                book = self.get_order_book()
                row = self._find_order_row(book, order_id)
                if row:
                    last_row = row
                    raw_status = (
                        row.get("orderstatus")
                        or row.get("orderStatus")
                        or row.get("status")
                        or row.get("order_state")
                    )
                    last_status = self._normalize_order_status(raw_status)
                    if last_status in self._TERMINAL_ORDER_STATUSES:
                        return last_status, last_row
                else:
                    # Order not present yet in orderbook.
                    last_status = "PENDING"
            except Exception as exc:
                # Do not hard-fail confirmation on transient orderbook fetch errors.
                logger.warning("confirm_order_terminal fetch failed order_id=%s err=%s", order_id, exc)

            if time.monotonic() >= deadline:
                return last_status or "PENDING", last_row

            time.sleep(max(0.05, float(poll_seconds)))

    # Place an order and wait briefly for terminal status confirmation.
    def place_order_confirmed(
        self,
        *,
        exchange: str,
        tradingsymbol: str,
        symboltoken: str,
        transaction_type: str,
        quantity: int,
        producttype: str = "INTRADAY",
        ordertype: str = "MARKET",
        variety: str = "NORMAL",
        duration: str = "DAY",
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        squareoff: float = 0.0,
        stoploss: float = 0.0,
        tag: Optional[str] = None,
        timeout_seconds: float = 8.0,
        poll_seconds: float = 0.8,
        tick_size: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Place an order and wait briefly for a terminal broker status.

        This prevents the system from assuming an order is filled just because
        placeOrder returned status=True.

        Returns a dict:
          {"place_order": <raw>, "order_id": "...", "terminal_status": "COMPLETE|...", "order_row": {...}}
        """
        place_resp = self.place_order(
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            symboltoken=symboltoken,
            transaction_type=transaction_type,
            quantity=quantity,
            producttype=producttype,
            ordertype=ordertype,
            variety=variety,
            duration=duration,
            price=price,
            trigger_price=trigger_price,
            squareoff=squareoff,
            stoploss=stoploss,
            tag=tag,
            tick_size=tick_size,
        )

        order_id = self._extract_order_id(place_resp)
        if not order_id:
            raise RuntimeError(f"placeOrder succeeded but order_id missing: {place_resp}")

        terminal_status, order_row = self.confirm_order_terminal(
            order_id,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )
        return {
            "place_order": place_resp,
            "order_id": order_id,
            "terminal_status": terminal_status,
            "order_row": order_row,
        }


__all__ = [
    "AngelOrderClient",
    "AngelHTTPBlockedError",
    "AngelHTTPResponseError",
]
