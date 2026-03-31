"""
Credential bundles for brokers.

For now we define AngelSecrets, which contains everything needed for
Angel One login (API key/secret, client code, PIN, TOTP, IP/MAC).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AngelSecrets:
    api_key: str
    api_secret: str
    client_code: str
    pin: str
    totp_secret: str
    client_local_ip: str
    client_public_ip: str
    mac_address: str


__all__ = ["AngelSecrets"]
