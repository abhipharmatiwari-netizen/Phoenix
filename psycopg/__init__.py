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


def connect(*args, **kwargs):
    """Delegate to real psycopg when available; otherwise raise a clear error.

    When the real psycopg[binary] package is installed (e.g. in CI after
    `pip install -r requirements.txt`), this shim delegates to it so tests
    that genuinely need a database connection can use one. In lightweight
    sandbox environments where psycopg is not installed, the RuntimeError
    prompts the test author to monkeypatch psycopg.connect instead.
    """
    import importlib
    import sys

    # Remove this module from sys.modules temporarily so importlib can find
    # the real psycopg in site-packages rather than the repo-root shim.
    _this = sys.modules.pop("psycopg", None)
    try:
        _real = importlib.import_module("psycopg")
        return _real.connect(*args, **kwargs)
    except ModuleNotFoundError:
        raise RuntimeError(
            "psycopg is not installed in this lightweight environment; "
            "tests should monkeypatch psycopg.connect or use app.data.postgres helpers"
        )
    finally:
        if _this is not None:
            sys.modules["psycopg"] = _this


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
