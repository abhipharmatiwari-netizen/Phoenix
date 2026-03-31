from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

_COMPAT_LOCAL_IP = "127.0.0.1"
_COMPAT_PUBLIC_IP = "127.0.0.1"
_COMPAT_MAC_ADDRESS = "AA:BB:CC:DD:EE:FF"
_PLACEHOLDER_TEXT_MARKERS = {
    "placeholder",
    "changeme",
    "replace",
    "setatdeploytime",
    "setbeforelive",
    "required",
    "yourpublicip",
    "yourlocalip",
    "yourmacaddress",
}
_BLOCKED_LIVE_MAC_VALUES = {
    "00:00:00:00:00:00",
    "aa:bb:cc:dd:ee:ff",
}
_MAC_ADDRESS_RE = re.compile(r"^[0-9a-f]{2}([:-][0-9a-f]{2}){5}$", re.IGNORECASE)


@dataclass(frozen=True)
class BrokerNetworkIdentity:
    client_local_ip: str
    client_public_ip: str
    mac_address: str


def is_live_trade_mode(trade_mode: object | None = None) -> bool:
    resolved = trade_mode if trade_mode is not None else os.getenv("TRADE_MODE", "PAPER")
    return str(resolved or "PAPER").strip().upper() == "LIVE"


def _normalize_text(value: object) -> str:
    return str(value or "").strip()


def _placeholder_token(value: object) -> str:
    lowered = _normalize_text(value).lower()
    return re.sub(r"[\s_\-]+", "", lowered)


def _looks_like_placeholder(value: object) -> bool:
    raw = _normalize_text(value)
    if not raw:
        return False
    if raw.startswith("<") and raw.endswith(">"):
        return True
    token = _placeholder_token(raw)
    return any(marker in token for marker in _PLACEHOLDER_TEXT_MARKERS)


def _ip_field_errors(env_name: str, value: object) -> list[str]:
    normalized = _normalize_text(value)
    if not normalized:
        return [f"{env_name} must be set in TRADE_MODE=LIVE"]
    if _looks_like_placeholder(normalized):
        return [f"{env_name} must not be a placeholder value in TRADE_MODE=LIVE"]
    if normalized.lower() == "localhost":
        return [f"{env_name} must not be localhost in TRADE_MODE=LIVE"]

    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        return [f"{env_name} must be a valid IP address in TRADE_MODE=LIVE"]

    if parsed.is_loopback or parsed.is_unspecified:
        return [
            f"{env_name} must not be loopback or unspecified in TRADE_MODE=LIVE"
        ]
    return []


def _mac_field_errors(value: object) -> list[str]:
    normalized = _normalize_text(value)
    if not normalized:
        return ["MAC_ADDRESS must be set in TRADE_MODE=LIVE"]
    if _looks_like_placeholder(normalized):
        return ["MAC_ADDRESS must not be a placeholder value in TRADE_MODE=LIVE"]
    if normalized.lower() in _BLOCKED_LIVE_MAC_VALUES:
        return ["MAC_ADDRESS must not use a fake default in TRADE_MODE=LIVE"]
    if not _MAC_ADDRESS_RE.fullmatch(normalized):
        return ["MAC_ADDRESS must be a valid MAC address in TRADE_MODE=LIVE"]
    return []


def live_broker_network_identity_env_errors(env: Mapping[str, Any]) -> list[str]:
    return live_broker_network_identity_errors(
        client_local_ip=env.get("CLIENT_LOCAL_IP"),
        client_public_ip=env.get("CLIENT_PUBLIC_IP"),
        mac_address=env.get("MAC_ADDRESS"),
    )


def live_broker_network_identity_errors(
    *,
    client_local_ip: object,
    client_public_ip: object,
    mac_address: object,
) -> list[str]:
    errors: list[str] = []
    errors.extend(_ip_field_errors("CLIENT_LOCAL_IP", client_local_ip))
    errors.extend(_ip_field_errors("CLIENT_PUBLIC_IP", client_public_ip))
    errors.extend(_mac_field_errors(mac_address))
    return errors


def resolve_broker_network_identity(
    *,
    client_local_ip: object,
    client_public_ip: object,
    mac_address: object,
    trade_mode: object | None = None,
    source: str,
) -> BrokerNetworkIdentity:
    live_mode = is_live_trade_mode(trade_mode)
    resolved_local_ip = _normalize_text(client_local_ip)
    resolved_public_ip = _normalize_text(client_public_ip)
    resolved_mac_address = _normalize_text(mac_address)

    if not live_mode:
        return BrokerNetworkIdentity(
            client_local_ip=resolved_local_ip or _COMPAT_LOCAL_IP,
            client_public_ip=resolved_public_ip or _COMPAT_PUBLIC_IP,
            mac_address=resolved_mac_address or _COMPAT_MAC_ADDRESS,
        )

    errors = live_broker_network_identity_errors(
        client_local_ip=resolved_local_ip,
        client_public_ip=resolved_public_ip,
        mac_address=resolved_mac_address,
    )
    if errors:
        details = "; ".join(errors)
        raise ValueError(
            f"{source} requires real broker network identity in TRADE_MODE=LIVE: {details}"
        )

    return BrokerNetworkIdentity(
        client_local_ip=resolved_local_ip,
        client_public_ip=resolved_public_ip,
        mac_address=resolved_mac_address,
    )


__all__ = [
    "BrokerNetworkIdentity",
    "is_live_trade_mode",
    "live_broker_network_identity_env_errors",
    "live_broker_network_identity_errors",
    "resolve_broker_network_identity",
]
