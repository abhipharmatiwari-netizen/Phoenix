"""Minimal google.auth shim for tests that monkeypatch ADC access."""

from __future__ import annotations

from .exceptions import DefaultCredentialsError


def default(*_args, **_kwargs):
    raise DefaultCredentialsError("Application Default Credentials are unavailable")


__all__ = ["DefaultCredentialsError", "default"]
