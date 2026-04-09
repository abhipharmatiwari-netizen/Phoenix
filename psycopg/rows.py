"""Minimal row factories used as sentinels in tests."""

from __future__ import annotations


def dict_row(value=None):
    return value


def tuple_row(value=None):
    return value


__all__ = ["dict_row", "tuple_row"]
