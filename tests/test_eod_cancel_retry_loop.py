from __future__ import annotations

from datetime import date, datetime, timezone
import logging
from types import SimpleNamespace

import pytest

from app.core.clock import SimulatedClock
from app.data.state_store import StateStore
from app.hub import exit_engines as exit_engines_module
from app.hub.exit_engines import EODExitEngine


class _UnexpectedOrderRouter:
    async def submit_order(self, **kwargs):
        _ = kwargs
        raise AssertionError("submit_order should not be called in cancel-loop tests")


class _CancelRunner:
    def __init__(self, outcomes):
        self.is_running = True
        self.tenant_id = "tenant-a"
        self.broker_account_id = "broker-a"
        self._outcomes = list(outcomes)
        self.cancel_calls = []

    async def cancel_open_intraday_orders(self, **kwargs):
        self.cancel_calls.append(dict(kwargs))
        if not self._outcomes:
            raise AssertionError("cancel_open_intraday_orders called too many times")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)


def _engine(*, retry_loop_enabled: bool, retry_on_no_eligible: bool) -> EODExitEngine:
    settings = SimpleNamespace(
        default_time_zone="Asia/Calcutta",
        enable_eod_exit=True,
        eod_exit_time="15:20",
        eod_exit_retry_cutoff_time="15:30",
        eod_exit_retry_on_no_eligible=retry_on_no_eligible,
        eod_exit_position_telemetry=False,
        eod_cancel_open_orders_enabled=True,
        eod_cancel_retry_loop_enabled=retry_loop_enabled,
    )
    clock = SimulatedClock(datetime(2026, 3, 21, 9, 55, tzinfo=timezone.utc))
    return EODExitEngine(
        settings=settings,
        state_store=StateStore(),
        order_router=_UnexpectedOrderRouter(),
        clock=clock,
    )


@pytest.fixture(autouse=True)
def _stub_sleep(monkeypatch):
    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(exit_engines_module.asyncio, "sleep", _no_sleep)


@pytest.mark.asyncio
async def test_eod_cancel_retry_loop_retries_transient_failure_then_succeeds(caplog):
    engine = _engine(retry_loop_enabled=True, retry_on_no_eligible=False)
    runner = _CancelRunner(
        [
            {"attempted": 1, "cancelled": 0, "failed": 1, "unsupported": 0},
            {"attempted": 1, "cancelled": 1, "failed": 0, "unsupported": 0},
        ]
    )
    caplog.set_level(logging.INFO)

    await engine.maybe_force_exit_all([runner])

    assert len(runner.cancel_calls) == 2
    assert engine._exited_today == date(2026, 3, 21)
    assert "event_type=EOD_CANCEL_OPEN_ORDERS_RETRY_ATTEMPT" in caplog.text
    assert "event_type=EOD_CANCEL_OPEN_ORDERS_RETRY_RESULT" in caplog.text


@pytest.mark.asyncio
async def test_eod_cancel_retry_loop_exhaustion_keeps_retry_window_open(caplog):
    engine = _engine(retry_loop_enabled=True, retry_on_no_eligible=True)
    runner = _CancelRunner(
        [
            {"attempted": 1, "cancelled": 0, "failed": 1, "unsupported": 0},
            {"attempted": 1, "cancelled": 0, "failed": 1, "unsupported": 0},
            {"attempted": 1, "cancelled": 0, "failed": 1, "unsupported": 0},
        ]
    )
    caplog.set_level(logging.INFO)

    await engine.maybe_force_exit_all([runner])

    assert len(runner.cancel_calls) == 3
    assert engine._exited_today is None
    assert "event_type=EOD_CANCEL_OPEN_ORDERS_RETRY_EXHAUSTED" in caplog.text
    assert "reason=order_cancel_failures" in caplog.text


@pytest.mark.asyncio
async def test_eod_cancel_retry_loop_is_bounded_on_repeated_exceptions(caplog):
    engine = _engine(retry_loop_enabled=True, retry_on_no_eligible=True)
    runner = _CancelRunner(
        [
            RuntimeError("first cancel timeout"),
            RuntimeError("second cancel timeout"),
            RuntimeError("third cancel timeout"),
        ]
    )
    caplog.set_level(logging.WARNING)

    await engine.maybe_force_exit_all([runner])

    assert len(runner.cancel_calls) == 3
    assert engine._exited_today is None
    assert "event_type=EOD_CANCEL_OPEN_ORDERS_FAILED" in caplog.text
