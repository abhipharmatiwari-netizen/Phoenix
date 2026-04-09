"""Lightweight psycopg compatibility shim for test environments.

This repository's tests exercise connection-string handling and monkeypatch the
connection entrypoint, but the real psycopg package is optional in the sandbox.
The shim keeps modules importable without pretending to be a full driver.
"""

from __future__ import annotations

from . import conninfo, rows, sql
from .conninfo import conninfo_to_dict, make_conninfo
from .rows import dict_row, tuple_row


class Error(Exception):
    """Base psycopg compatibility error."""


class OperationalError(Error):
    """Operational error placeholder."""


class InterfaceError(Error):
    """Interface error placeholder."""


class ProgrammingError(Error):
    """Programming error placeholder."""


def connect(*_args, **_kwargs):
    raise RuntimeError(
        "psycopg is not installed in this lightweight environment; "
        "tests should monkeypatch psycopg.connect or use app.data.postgres helpers"
    )


__all__ = [
    "Error",
    "OperationalError",
    "InterfaceError",
    "ProgrammingError",
    "conninfo",
    "conninfo_to_dict",
    "connect",
    "dict_row",
    "make_conninfo",
    "rows",
    "sql",
    "tuple_row",
]
