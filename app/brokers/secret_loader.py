"""
Secret loader for broker credential bundles.

For now:
- Load AngelSecrets from Google Secret Manager using BrokerAccountModel.secret_ref.
- Fallback to env-based ANGEL_* values only in non-LIVE compatibility paths.

Per-account secret resolution:
- BrokerAccountModel.secret_ref is a logical key.
- Settings may define BROKER_SECRET_PROJECT_ID and BROKER_SECRET_PREFIX.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any, Dict, Optional

try:  # pragma: no cover - optional in lightweight test environments
    from google.cloud import secretmanager  # type: ignore
except Exception:  # pragma: no cover - optional in lightweight test environments
    secretmanager = None

from app.config.settings import get_settings
from app.brokers.secrets import AngelSecrets
from app.core.broker_network_identity import resolve_broker_network_identity
from app.tenants.models import BrokerAccountModel

logger = logging.getLogger(__name__)

_LIVE_SECRET_BACKENDS = {
    "secret_manager",
    "secretmanager",
    "postgres",
    "pg",
    "db",
    "database",
}
_REQUIRED_POSTGRES_CREDENTIAL_FIELDS = (
    "api_key",
    "client_code",
    "pin",
    "totp_secret",
)


@lru_cache(maxsize=1)
def get_secret_manager_client():
    if secretmanager is None:
        raise RuntimeError("google-cloud-secret-manager is required for secret manager backends")
    return secretmanager.SecretManagerServiceClient()


def _build_secret_name(secret_id: str) -> str:
    settings = get_settings()
    project_id = settings.broker_secret_project_id or settings.gcp_project_id
    if not project_id:
        raise RuntimeError("No project id configured for Secret Manager")

    if settings.broker_secret_prefix:
        secret_id = f"{settings.broker_secret_prefix}{secret_id}"

    return f"projects/{project_id}/secrets/{secret_id}/versions/latest"


def _fetch_secret_payload(secret_ref: str) -> Optional[Dict[str, Any]]:
    client = get_secret_manager_client()
    name = _build_secret_name(secret_ref)
    try:
        response = client.access_secret_version(name=name)
        data = response.payload.data.decode("utf-8")
        obj = json.loads(data)
        if not isinstance(obj, dict):
            logger.error("Secret %s payload is not a JSON object", name)
            return None
        return obj
    except Exception:
        logger.exception("Failed to access or parse secret %s", name)
        return None


def _fetch_secret_payload_from_postgres(
    broker_account_id: str,
) -> Optional[Dict[str, Any]]:
    from app.data.postgres import get_control_plane_dsn, connect_with_retry

    sql = """
        SELECT
            api_key,
            api_secret,
            client_code,
            pin,
            totp_secret,
            client_local_ip,
            client_public_ip,
            mac_address,
            state
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
                return {
                    "api_key": row[0],
                    "api_secret": row[1],
                    "client_code": row[2],
                    "pin": row[3],
                    "totp_secret": row[4],
                    "client_local_ip": row[5],
                    "client_public_ip": row[6],
                    "mac_address": row[7],
                    "state": row[8],
                }
    except Exception:
        logger.exception(
            "Failed to load broker credentials from Postgres for broker_account_id=%s",
            broker_account_id,
        )
        return None


def _missing_required_postgres_credential_fields(
    payload: Dict[str, Any],
) -> list[str]:
    return [
        field
        for field in _REQUIRED_POSTGRES_CREDENTIAL_FIELDS
        if not str(payload.get(field) or "").strip()
    ]


def load_angel_secrets_from_secret_manager(
    account: BrokerAccountModel,
) -> Optional[AngelSecrets]:
    if not account.secret_ref:
        return None

    payload = _fetch_secret_payload(account.secret_ref)
    if payload is None:
        return None

    try:
        api_key = str(payload["api_key"])
        api_secret = str(payload["api_secret"])
        pin = str(payload["pin"])
        totp_secret = str(payload["totp_secret"])
    except KeyError as exc:
        logger.error("Secret %s missing required key: %s", account.secret_ref, exc)
        return None

    client_code = str(payload.get("client_code") or account.client_code)
    network_identity = resolve_broker_network_identity(
        client_local_ip=payload.get("client_local_ip"),
        client_public_ip=payload.get("client_public_ip"),
        mac_address=payload.get("mac_address"),
        trade_mode=_trade_mode(),
        source=(
            "Secret Manager broker credentials "
            f"for broker_account_id={account.broker_account_id}"
        ),
    )

    logger.info(
        "Loaded Angel secrets from Secret Manager for broker_account_id=%s secret_ref=%s",
        account.broker_account_id,
        account.secret_ref,
    )

    return AngelSecrets(
        api_key=api_key,
        api_secret=api_secret,
        client_code=client_code,
        pin=pin,
        totp_secret=totp_secret,
        client_local_ip=network_identity.client_local_ip,
        client_public_ip=network_identity.client_public_ip,
        mac_address=network_identity.mac_address,
    )


def load_angel_secrets_from_postgres(
    account: BrokerAccountModel,
) -> Optional[AngelSecrets]:
    payload = _fetch_secret_payload_from_postgres(account.broker_account_id)
    if payload is None:
        return None

    missing_fields = _missing_required_postgres_credential_fields(payload)
    if missing_fields:
        logger.error(
            "Postgres broker_credentials missing non-empty required fields "
            "for broker_account_id=%s: %s",
            account.broker_account_id,
            ", ".join(missing_fields),
        )
        return None

    try:
        api_key = str(payload["api_key"])
        api_secret = str(payload.get("api_secret", ""))
        pin = str(payload["pin"])
        totp_secret = str(payload["totp_secret"])
    except KeyError as exc:
        logger.error(
            "Postgres broker_credentials missing required key for broker_account_id=%s: %s",
            account.broker_account_id,
            exc,
        )
        return None

    client_code = str(payload.get("client_code") or account.client_code)
    network_identity = resolve_broker_network_identity(
        client_local_ip=payload.get("client_local_ip"),
        client_public_ip=payload.get("client_public_ip"),
        mac_address=payload.get("mac_address"),
        trade_mode=_trade_mode(),
        source=(
            "Postgres broker credentials "
            f"for broker_account_id={account.broker_account_id}"
        ),
    )

    logger.info(
        "Loaded Angel secrets from Postgres for broker_account_id=%s",
        account.broker_account_id,
    )

    return AngelSecrets(
        api_key=api_key,
        api_secret=api_secret,
        client_code=client_code,
        pin=pin,
        totp_secret=totp_secret,
        client_local_ip=network_identity.client_local_ip,
        client_public_ip=network_identity.client_public_ip,
        mac_address=network_identity.mac_address,
    )


def load_angel_secrets_from_env(account: BrokerAccountModel) -> AngelSecrets:
    api_key = os.getenv("ANGEL_API_KEY", "")
    api_secret = os.getenv("ANGEL_API_SECRET", "")
    pin = os.getenv("ANGEL_CLIENT_PIN", "")
    totp_secret = os.getenv("ANGEL_TOTP_SECRET", "")
    client_code = account.client_code or os.getenv("ANGEL_CLIENT_ID", "")

    network_identity = resolve_broker_network_identity(
        client_local_ip=os.getenv("CLIENT_LOCAL_IP"),
        client_public_ip=os.getenv("CLIENT_PUBLIC_IP"),
        mac_address=os.getenv("MAC_ADDRESS"),
        trade_mode=_trade_mode(),
        source=f"environment broker credentials for broker_account_id={account.broker_account_id}",
    )

    return AngelSecrets(
        api_key=api_key,
        api_secret=api_secret,
        client_code=client_code,
        pin=pin,
        totp_secret=totp_secret,
        client_local_ip=network_identity.client_local_ip,
        client_public_ip=network_identity.client_public_ip,
        mac_address=network_identity.mac_address,
    )


def _trade_mode() -> str:
    return str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper()


def _is_live_trade_mode() -> bool:
    return _trade_mode() == "LIVE"


def _raise_live_secret_resolution_error(
    *,
    backend: str,
    broker_account_id: str,
    detail: str,
) -> None:
    message = (
        "TRADE_MODE=LIVE broker secret resolution failed "
        f"backend={backend or '(empty)'} broker_account_id={broker_account_id} "
        f"detail={detail}"
    )
    logger.error(message)
    raise RuntimeError(message)


def resolve_angel_secrets(account: BrokerAccountModel) -> AngelSecrets:
    settings = get_settings()
    backend = (settings.broker_secret_backend or "secret_manager").strip().lower()
    live_mode = _is_live_trade_mode()

    if live_mode and backend not in _LIVE_SECRET_BACKENDS:
        _raise_live_secret_resolution_error(
            backend=backend,
            broker_account_id=str(account.broker_account_id),
            detail=(
                "TRADE_MODE=LIVE requires BROKER_SECRET_BACKEND to be one of: "
                "secret_manager, postgres"
            ),
        )

    if backend in {"postgres", "pg", "db", "database"}:
        secrets = load_angel_secrets_from_postgres(account)
        if secrets is not None:
            if live_mode:
                resolve_broker_network_identity(
                    client_local_ip=secrets.client_local_ip,
                    client_public_ip=secrets.client_public_ip,
                    mac_address=secrets.mac_address,
                    trade_mode="LIVE",
                    source=(
                        "resolved postgres broker credentials "
                        f"for broker_account_id={account.broker_account_id}"
                    ),
                )
            return secrets
        if live_mode:
            _raise_live_secret_resolution_error(
                backend=backend,
                broker_account_id=str(account.broker_account_id),
                detail="postgres credentials unavailable and env fallback is forbidden",
            )
        logger.warning(
            "Postgres secrets missing; falling back to env-based Angel secrets for broker_account_id=%s",
            account.broker_account_id,
        )
        return load_angel_secrets_from_env(account)

    if backend == "env":
        secrets = load_angel_secrets_from_env(account)
        if live_mode:
            resolve_broker_network_identity(
                client_local_ip=secrets.client_local_ip,
                client_public_ip=secrets.client_public_ip,
                mac_address=secrets.mac_address,
                trade_mode="LIVE",
                source=(
                    "resolved env broker credentials "
                    f"for broker_account_id={account.broker_account_id}"
                ),
            )
        return secrets

    if backend == "auto":
        secrets = load_angel_secrets_from_postgres(account)
        if secrets is not None:
            if live_mode:
                resolve_broker_network_identity(
                    client_local_ip=secrets.client_local_ip,
                    client_public_ip=secrets.client_public_ip,
                    mac_address=secrets.mac_address,
                    trade_mode="LIVE",
                    source=(
                        "resolved postgres broker credentials "
                        f"for broker_account_id={account.broker_account_id}"
                    ),
                )
            return secrets
        secrets = load_angel_secrets_from_secret_manager(account)
        if secrets is not None:
            if live_mode:
                resolve_broker_network_identity(
                    client_local_ip=secrets.client_local_ip,
                    client_public_ip=secrets.client_public_ip,
                    mac_address=secrets.mac_address,
                    trade_mode="LIVE",
                    source=(
                        "resolved secret_manager broker credentials "
                        f"for broker_account_id={account.broker_account_id}"
                    ),
                )
            return secrets
        if live_mode:
            _raise_live_secret_resolution_error(
                backend=backend,
                broker_account_id=str(account.broker_account_id),
                detail=(
                    "no credentials resolved from postgres or secret_manager and "
                    "env fallback is forbidden"
                ),
            )
        logger.warning(
            "Falling back to env-based Angel secrets for broker_account_id=%s",
            account.broker_account_id,
        )
        return load_angel_secrets_from_env(account)

    secrets = load_angel_secrets_from_secret_manager(account)
    if secrets is not None:
        if live_mode:
            resolve_broker_network_identity(
                client_local_ip=secrets.client_local_ip,
                client_public_ip=secrets.client_public_ip,
                mac_address=secrets.mac_address,
                trade_mode="LIVE",
                source=(
                    "resolved secret_manager broker credentials "
                    f"for broker_account_id={account.broker_account_id}"
                ),
            )
        return secrets

    if live_mode:
        _raise_live_secret_resolution_error(
            backend=backend,
            broker_account_id=str(account.broker_account_id),
            detail="secret_manager credentials unavailable and env fallback is forbidden",
        )
    logger.warning(
        "Falling back to env-based Angel secrets for broker_account_id=%s",
        account.broker_account_id,
    )
    return load_angel_secrets_from_env(account)


__all__ = [
    "get_secret_manager_client",
    "load_angel_secrets_from_postgres",
    "load_angel_secrets_from_secret_manager",
    "load_angel_secrets_from_env",
    "resolve_angel_secrets",
]
