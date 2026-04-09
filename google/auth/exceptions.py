"""Minimal exception types used by optional Google auth imports."""


class DefaultCredentialsError(Exception):
    """Raised when ADC is unavailable in lightweight environments."""


__all__ = ["DefaultCredentialsError"]
