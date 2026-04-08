"""
Replay isolation context for place_order_via_bridge.

Provides thread-local flag and sink management so the strategy bridge can
short-circuit into replay-local execution instead of touching live hub
infrastructure.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any, Generator, Optional

REPLAY_ISOLATED_EXECUTION_ENV = "REPLAY_ISOLATED_EXECUTION_ENV"

_local = threading.local()


def get_replay_flag() -> bool:
    """Return True if the current thread has isolated replay enabled."""
    return bool(getattr(_local, "replay_isolated", False))


def get_replay_sink() -> Optional[Any]:
    """Return the active replay order sink for this thread, or None."""
    return getattr(_local, "replay_sink", None)


@contextmanager
def isolated_replay_flag(flag: bool) -> Generator[None, None, None]:
    """Context manager that activates/deactivates the isolated replay flag.

    Also sets/clears the REPLAY_ISOLATED_EXECUTION_ENV environment variable so
    that subprocesses and env-var-based checks see a consistent value.
    """
    prev_flag = getattr(_local, "replay_isolated", False)
    prev_env = os.environ.get(REPLAY_ISOLATED_EXECUTION_ENV)
    _local.replay_isolated = flag
    if flag:
        os.environ[REPLAY_ISOLATED_EXECUTION_ENV] = "1"
    else:
        os.environ.pop(REPLAY_ISOLATED_EXECUTION_ENV, None)
    try:
        yield
    finally:
        _local.replay_isolated = prev_flag
        if prev_env is None:
            os.environ.pop(REPLAY_ISOLATED_EXECUTION_ENV, None)
        else:
            os.environ[REPLAY_ISOLATED_EXECUTION_ENV] = prev_env


@contextmanager
def isolated_replay_order_sink(sink: Any) -> Generator[None, None, None]:
    """Context manager that installs a replay order sink and activates isolation.

    The bridge will route all place_order_via_bridge calls to sink.place_order()
    while this context is active.
    """
    prev_sink = getattr(_local, "replay_sink", None)
    prev_flag = getattr(_local, "replay_isolated", False)
    prev_env = os.environ.get(REPLAY_ISOLATED_EXECUTION_ENV)
    _local.replay_sink = sink
    _local.replay_isolated = True
    os.environ[REPLAY_ISOLATED_EXECUTION_ENV] = "1"
    try:
        yield
    finally:
        _local.replay_sink = prev_sink
        _local.replay_isolated = prev_flag
        if prev_env is None:
            os.environ.pop(REPLAY_ISOLATED_EXECUTION_ENV, None)
        else:
            os.environ[REPLAY_ISOLATED_EXECUTION_ENV] = prev_env


__all__ = [
    "REPLAY_ISOLATED_EXECUTION_ENV",
    "get_replay_flag",
    "get_replay_sink",
    "isolated_replay_flag",
    "isolated_replay_order_sink",
]
