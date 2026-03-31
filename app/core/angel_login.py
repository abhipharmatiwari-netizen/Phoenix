"""Implements parts of §3 Broker Adapter Layer (legacy Angel single-tenant login helper)."""

# Legacy helper for Angel login using env secrets and TOTP.
# Provides token generation and best-effort logout for the broker session.
import http.client
import json
import pyotp
import os
import logging
import random
import re
import time
from dataclasses import dataclass
from json import JSONDecodeError

from dotenv import load_dotenv
from app.core.broker_network_identity import resolve_broker_network_identity
from app.core.rate_limiter import rate_limiter
from app.brokers.secrets import AngelSecrets
from app.config.settings import get_settings
from pathlib import Path

load_dotenv()

def _env_or_file(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.getenv(name)
    if value not in (None, ""):
        return value

    file_path = os.getenv(f"{name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except Exception as exc:
            raise ValueError(f"Unable to read secret file for {name}: {file_path} ({exc})") from exc

    if required:
        raise ValueError(f"{name} environment variable not set")
    return default

# ---- CONFIG (legacy single-tenant env wiring) ----
API_KEY = _env_or_file("ANGEL_API_KEY", "ANGEL_API_KEY")
CLIENT_CODE = _env_or_file("ANGEL_CLIENT_CODE", "ANGEL_CLIENT_CODE")
CLIENT_PIN = _env_or_file("ANGEL_CLIENT_PIN", "ANGEL_CLIENT_PIN")
totp_secret = _env_or_file("ANGEL_TOTP_SECRET", "ANGEL_TOTP_SECRET")
STATE_VARIABLE = "WEB"
# -------------------------------------------------------------


logger = logging.getLogger("angel_login")
"""Module logger for Angel login helper."""

_RETRYABLE_LOGIN_DELAY_SECONDS = (0.2, 0.8)
_LOGIN_SNIPPET_LIMIT = 200
_RATE_LIMIT_TEXT_RE = re.compile(r"exceeding access rate", flags=re.IGNORECASE)
_STRICT_POSTGRES_SECRET_BACKENDS = {"postgres", "pg", "db", "database"}
_POSTGRES_SECRET_BACKENDS = _STRICT_POSTGRES_SECRET_BACKENDS | {"auto"}


# Shorten verbose logs to a single-line snippet.
def _truncate_log(text: str, limit: int = 512) -> str:
    """Return a single-line snippet truncated to a maximum length."""
    compact = " ".join(text.strip().split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}...(truncated)"


def _safe_response_snippet(text: str, limit: int = _LOGIN_SNIPPET_LIMIT) -> str:
    return _truncate_log(text, limit=limit)


def _is_rate_limited_message(text: str) -> bool:
    return bool(_RATE_LIMIT_TEXT_RE.search(text or ""))


def _is_retryable_login_response(status_code: int | None, payload_text: str) -> bool:
    if status_code is None:
        return False
    if status_code >= 500:
        return True
    if status_code == 429:
        return True
    return status_code == 403 and _is_rate_limited_message(payload_text)


def _looks_like_retryable_business_failure(payload: dict) -> bool:
    data = payload.get("data")
    if isinstance(data, dict):
        if _is_rate_limited_message(str(data.get("message", ""))):
            return True
    return _is_rate_limited_message(
        " ".join(
            str(payload.get(key, ""))
            for key in ("message", "errorcode", "errorCode", "details")
        )
    )


def _broker_secret_backend_mode() -> str:
    settings = get_settings()
    return str(settings.broker_secret_backend or "secret_manager").strip().lower()


def _resolve_env_network_identity(source: str):
    return resolve_broker_network_identity(
        client_local_ip=_env_or_file("CLIENT_LOCAL_IP"),
        client_public_ip=_env_or_file("CLIENT_PUBLIC_IP"),
        mac_address=_env_or_file("MAC_ADDRESS"),
        trade_mode=os.getenv("TRADE_MODE", "PAPER"),
        source=source,
    )


def _request_json_with_retry(
    *,
    conn: http.client.HTTPSConnection,
    method: str,
    path: str,
    body: str,
    headers: dict[str, str],
    limiter_key: str,
    operation: str,
    max_attempts: int = 2,
) -> dict:
    attempts = max(1, int(max_attempts))
    for attempt in range(1, attempts + 1):
        rate_limiter.acquire(limiter_key)
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        status_code = int(getattr(response, "status", 0) or 0)
        body_text = response.read().decode("utf-8", errors="replace")

        if status_code < 200 or status_code >= 300:
            if attempt < attempts and _is_retryable_login_response(
                status_code, body_text
            ):
                sleep_seconds = random.uniform(
                    _RETRYABLE_LOGIN_DELAY_SECONDS[0],
                    _RETRYABLE_LOGIN_DELAY_SECONDS[1],
                )
                logger.warning(
                    "event_type=ANGEL_LOGIN_RETRY operation=%s reason=http_status status=%s attempt=%d/%d sleep=%.3fs snippet=%s",
                    operation,
                    status_code,
                    attempt,
                    attempts,
                    sleep_seconds,
                    _safe_response_snippet(body_text) or "-",
                )
                time.sleep(sleep_seconds)
                continue
            raise RuntimeError(
                f"{operation} failed: http_status={status_code} body={_safe_response_snippet(body_text)}"
            )

        try:
            payload = json.loads(body_text)
        except JSONDecodeError:
            raise RuntimeError(
                f"{operation} failed: invalid_json body={_safe_response_snippet(body_text)}"
            )

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"{operation} failed: invalid_payload_type={type(payload).__name__}"
            )

        if payload.get("status"):
            return payload

        if attempt < attempts and _looks_like_retryable_business_failure(payload):
            sleep_seconds = random.uniform(
                _RETRYABLE_LOGIN_DELAY_SECONDS[0],
                _RETRYABLE_LOGIN_DELAY_SECONDS[1],
            )
            logger.warning(
                "event_type=ANGEL_LOGIN_RETRY operation=%s reason=api_status_false attempt=%d/%d sleep=%.3fs payload=%s",
                operation,
                attempt,
                attempts,
                sleep_seconds,
                _safe_response_snippet(str(payload)),
            )
            time.sleep(sleep_seconds)
            continue

        raise RuntimeError(f"{operation} failed: {payload}")

    raise RuntimeError(f"{operation} failed unexpectedly after retry loop")


@dataclass(frozen=True)
class AngelLoginTokens:
    """Container for Angel login tokens and identifiers."""
    jwt_token: str
    refresh_token: str
    feed_token: str
    client_code: str
    api_key: str


def _load_postgres_secrets_for_default_account() -> AngelSecrets | None:
    """Load default broker secrets from Postgres when that legacy backend is configured."""
    settings = get_settings()
    backend = _broker_secret_backend_mode()
    if backend not in _POSTGRES_SECRET_BACKENDS:
        return None

    broker_account_id = settings.hub_default_broker_account_id or os.getenv(
        "HUB_DEFAULT_BROKER_ACCOUNT_ID", ""
    )
    if not broker_account_id:
        logger.warning(
            "No HUB_DEFAULT_BROKER_ACCOUNT_ID set; skipping Postgres secret lookup."
        )
        return None

    try:
        from app.data.postgres import get_control_plane_dsn, connect_with_retry
    except Exception:
        logger.exception("Postgres client unavailable; skipping secret lookup.")
        return None

    sql = """
        SELECT
            api_key,
            api_secret,
            client_code,
            pin,
            totp_secret,
            client_local_ip,
            client_public_ip,
            mac_address
        FROM broker_credentials
        WHERE broker_account_id = %s
    """
    try:
        with connect_with_retry(get_control_plane_dsn()) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (broker_account_id,))
                row = cur.fetchone()
                if row is None:
                    return None
                network_identity = resolve_broker_network_identity(
                    client_local_ip=row[5],
                    client_public_ip=row[6],
                    mac_address=row[7],
                    trade_mode=os.getenv("TRADE_MODE", "PAPER"),
                    source=(
                        "legacy postgres broker credentials "
                        f"for broker_account_id={broker_account_id}"
                    ),
                )
                return AngelSecrets(
                    api_key=str(row[0]),
                    api_secret=str(row[1] or ""),
                    client_code=str(row[2]),
                    pin=str(row[3]),
                    totp_secret=str(row[4]),
                    client_local_ip=network_identity.client_local_ip,
                    client_public_ip=network_identity.client_public_ip,
                    mac_address=network_identity.mac_address,
                )
    except ValueError:
        raise
    except Exception:
        logger.exception(
            "Failed to load broker_credentials for broker_account_id=%s",
            broker_account_id,
        )
        return None


# Log in with env secrets and return JWT/feed tokens.
def angel_login_and_get_tokens():
    """Log in using the legacy env or Postgres-backed secret path and return tokens."""
    settings = get_settings()
    backend = _broker_secret_backend_mode()
    strict_postgres_backend = backend in _STRICT_POSTGRES_SECRET_BACKENDS
    broker_account_id = settings.hub_default_broker_account_id or os.getenv(
        "HUB_DEFAULT_BROKER_ACCOUNT_ID", ""
    )

    secrets = _load_postgres_secrets_for_default_account()
    if secrets is not None:
        resolve_broker_network_identity(
            client_local_ip=secrets.client_local_ip,
            client_public_ip=secrets.client_public_ip,
            mac_address=secrets.mac_address,
            trade_mode=os.getenv("TRADE_MODE", "PAPER"),
            source=(
                "legacy resolved broker credentials "
                f"for broker_account_id={broker_account_id or 'default'}"
            ),
        )
        tokens = angel_login_with_secrets(secrets)
        return {
            "jwtToken": tokens.jwt_token,
            "feedToken": tokens.feed_token,
            "clientCode": tokens.client_code,
            "API_KEY": tokens.api_key,
            "client_local_ip": secrets.client_local_ip,
            "client_public_ip": secrets.client_public_ip,
            "mac_address": secrets.mac_address,
        }

    if strict_postgres_backend:
        if not broker_account_id:
            raise ValueError(
                "BROKER_SECRET_BACKEND=postgres requires HUB_DEFAULT_BROKER_ACCOUNT_ID"
            )
        raise ValueError(
            "BROKER_SECRET_BACKEND=postgres but broker_credentials lookup returned no usable secrets "
            f"for broker_account_id={broker_account_id}. Check CONTROL_PLANE_PG_* settings and broker_credentials rows."
        )

    api_key = _env_or_file("ANGEL_API_KEY", "ANGEL_API_KEY")
    client_code = _env_or_file("ANGEL_CLIENT_CODE", "ANGEL_CLIENT_CODE")
    client_pin = _env_or_file("ANGEL_CLIENT_PIN", "ANGEL_CLIENT_PIN")
    network_identity = _resolve_env_network_identity("legacy env broker credentials")

    totp_secret = _env_or_file("ANGEL_TOTP_SECRET", required=True)
    if not totp_secret:
        raise ValueError("ANGEL_TOTP_SECRET environment variable not set")

    # 2) Generate TOTP using the *local* variable
    current_totp = pyotp.TOTP(totp_secret).now()

    conn = http.client.HTTPSConnection("apiconnect.angelone.in")

    # ---- 3) LOGIN (clientcode + password + TOTP) ----
    login_payload = json.dumps(
        {
            "clientcode": client_code,
            "password": client_pin,
            "totp": current_totp,
            "state": STATE_VARIABLE,
        }
    )

    login_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": network_identity.client_local_ip,
        "X-ClientPublicIP": network_identity.client_public_ip,
        "X-MACAddress": network_identity.mac_address,
        "X-PrivateKey": api_key,
    }

    login_data = _request_json_with_retry(
        conn=conn,
        method="POST",
        path="/rest/auth/angelbroking/user/v1/loginByPassword",
        body=login_payload,
        headers=login_headers,
        limiter_key="loginByPassword",
        operation="ANGEL_LOGIN",
        max_attempts=2,
    )

    jwt_token = login_data["data"]["jwtToken"]
    refresh_token = login_data["data"]["refreshToken"]

    logger.info("Login OK")
    # print("jwtToken     :", jwt_token)
    # print("refreshToken :", refresh_token)

    # ---- 4) OPTIONAL: generateTokens call using refresh token ----
    jwt_payload = json.dumps({"refreshToken": refresh_token})

    jwt_headers = {
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

    jwt_data = _request_json_with_retry(
        conn=conn,
        method="POST",
        path="/rest/auth/angelbroking/jwt/v1/generateTokens",
        body=jwt_payload,
        headers=jwt_headers,
        limiter_key="generateTokens",
        operation="ANGEL_GENERATE_TOKENS",
        max_attempts=2,
    )

    jwt_token = jwt_data["data"]["jwtToken"]  # raw JWT (no 'Bearer ' prefix)
    feed_token = jwt_data["data"]["feedToken"]  # <- IMPORTANT for WebSocket 2.0

    logger.info("Login + generateTokens OK")
    short_jwt = f"{jwt_token[:6]}...{jwt_token[-4:]}" if len(jwt_token) > 10 else "***"
    logger.info("jwtToken (masked): %s (len=%d)", short_jwt, len(jwt_token))
    logger.info("feedToken obtained (len=%d)", len(feed_token))

    return {
        "jwtToken": jwt_token,
        "feedToken": feed_token,
        "clientCode": client_code,
        "API_KEY": api_key,
        "client_local_ip": network_identity.client_local_ip,
        "client_public_ip": network_identity.client_public_ip,
        "mac_address": network_identity.mac_address,
    }


# Log in using provided secrets and return a token bundle.
def angel_login_with_secrets(secrets: AngelSecrets) -> AngelLoginTokens:
    """
    Login using explicitly provided secrets instead of env/global constants.
    """
    current_totp = pyotp.TOTP(secrets.totp_secret).now()

    conn = http.client.HTTPSConnection("apiconnect.angelone.in")

    login_payload = json.dumps(
        {
            "clientcode": secrets.client_code,
            "password": secrets.pin,
            "totp": current_totp,
            "state": STATE_VARIABLE,
        }
    )

    login_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": secrets.client_local_ip,
        "X-ClientPublicIP": secrets.client_public_ip,
        "X-MACAddress": secrets.mac_address,
        "X-PrivateKey": secrets.api_key,
    }

    login_data = _request_json_with_retry(
        conn=conn,
        method="POST",
        path="/rest/auth/angelbroking/user/v1/loginByPassword",
        body=login_payload,
        headers=login_headers,
        limiter_key="loginByPassword",
        operation="ANGEL_LOGIN",
        max_attempts=2,
    )

    jwt_token = login_data["data"]["jwtToken"]
    refresh_token = login_data["data"]["refreshToken"]

    jwt_payload = json.dumps({"refreshToken": refresh_token})

    jwt_headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": secrets.client_local_ip,
        "X-ClientPublicIP": secrets.client_public_ip,
        "X-MACAddress": secrets.mac_address,
        "X-PrivateKey": secrets.api_key,
    }

    jwt_data = _request_json_with_retry(
        conn=conn,
        method="POST",
        path="/rest/auth/angelbroking/jwt/v1/generateTokens",
        body=jwt_payload,
        headers=jwt_headers,
        limiter_key="generateTokens",
        operation="ANGEL_GENERATE_TOKENS",
        max_attempts=2,
    )

    jwt_token = jwt_data["data"]["jwtToken"]
    feed_token = jwt_data["data"]["feedToken"]

    return AngelLoginTokens(
        jwt_token=jwt_token,
        refresh_token=refresh_token,
        feed_token=feed_token,
        client_code=secrets.client_code,
        api_key=secrets.api_key,
    )


# Call Angel logout endpoint to close the session.
def angel_logout(jwt_token: str, api_key: str = API_KEY) -> bool:
    """
    Best-effort logout to close the session. Rate limited per Angel guidelines.
    """
    network_identity = _resolve_env_network_identity("legacy env broker logout")
    conn = http.client.HTTPSConnection("apiconnect.angelone.in")
    payload = json.dumps({"clientcode": CLIENT_CODE})
    headers = {
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
    try:
        rate_limiter.acquire("logout")
        conn.request(
            "POST",
            "/rest/secure/angelbroking/user/v1/logout",
            body=payload,
            headers=headers,
        )
        res = conn.getresponse()
        text = res.read().decode("utf-8")
        logger.debug("Logout response: %s", _truncate_log(text))
        data = json.loads(text)
        return bool(data.get("status"))
    except Exception as exc:  # pragma: no cover - best effort
        logger.error("Logout failed: %s", exc)
        return False


if __name__ == "__main__":
    tokens = angel_login_and_get_tokens()
    # Example logout (best effort):
    angel_logout(tokens["jwtToken"])
