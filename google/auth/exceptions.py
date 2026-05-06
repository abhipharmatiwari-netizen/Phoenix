"""Minimal google.auth exceptions shim for test environments.

Mirrors the surface of google.auth.exceptions from google-auth >= 2.40.0
so that modules importing google.api_core (which references TransportError
in retry_base.py) can be collected by pytest without AttributeError.
"""


class DefaultCredentialsError(Exception):
    """Raised when ADC is unavailable in lightweight environments."""


class TransportError(Exception):
    """Raised on transport-level failures (google-auth >= 2.40.0)."""


class GoogleAuthError(Exception):
    """Base class for google.auth errors."""


__all__ = ["DefaultCredentialsError", "GoogleAuthError", "TransportError"]
