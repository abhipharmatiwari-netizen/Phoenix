"""Wrapper around scripts.replay.run_replay that silences per-bar INFO logs.

The strategy modules use stdlib `logging` heavily — running the optimize sweep
without suppression dumps ~50K lines/min of per-bar diagnostics that crater
disk I/O. This wrapper installs a root-level filter at WARNING and only lets
the replay summary/diagnostics through at INFO+.

Usage matches scripts.replay.run_replay one-for-one.
"""
from __future__ import annotations

import logging
import os
import sys

# Ensure project root is on sys.path even when invoked as a script.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _silence_app_loggers() -> None:
    # Silence everything by default.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Keep the replay top-level summary visible at INFO.
    logging.getLogger("replay").setLevel(logging.INFO)
    logging.getLogger("scripts.replay").setLevel(logging.INFO)
    # Quiet noisy strategy/runtime loggers.
    for noisy in (
        "app",
        "app.strategies",
        "app.strategies.ema20_strategy",
        "app.runners",
        "app.hub",
        "app.orders",
        "app.pnl",
        "app.core",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


if __name__ == "__main__":
    _silence_app_loggers()
    # Now hand off to the original CLI.
    import runpy
    sys.argv[0] = "scripts.replay.run_replay"
    runpy.run_module("scripts.replay.run_replay", run_name="__main__")
