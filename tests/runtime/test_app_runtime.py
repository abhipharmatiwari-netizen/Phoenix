from __future__ import annotations

import time
from types import SimpleNamespace
import sys

import pytest

from app.core.instrument_control import InstrumentController
from app.core.strategy_switch import StrategySwitchboard
from app.runtime import app_runtime
from app.runtime.app_runtime import AppRuntime, StreamWorker, WorkerWatchdog


class _DummyLoop:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self._running = False
        self.configured_interval = None

    def start(self) -> None:
        self.started += 1
        self._running = True

    def stop(self, timeout: float = 0) -> None:
        self.stopped += 1
        self._running = False

    def configure_interval(self, interval_seconds: float) -> None:
        self.configured_interval = interval_seconds

    def running(self) -> bool:
        return self._running


def _attach_dummy_workers(runtime) -> None:
    runtime.stream_worker = _DummyLoop()
    runtime.watchdog = _DummyLoop()
    runtime.alert_evaluator = _DummyLoop()


class _DummyHub:
    def __init__(self) -> None:
        self.initialized = 0
        self.started = 0
        self.stopped = 0
        self.startup_waits = 0

    async def initialize(self) -> None:
        self.initialized += 1

    async def start_all(self) -> None:
        self.started += 1

    async def wait_for_runner_startup(self) -> None:
        self.startup_waits += 1

    async def stop_all(self) -> None:
        self.stopped += 1


class _DummyLifecycle:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


class _DummyOrderRouter:
    def __init__(self) -> None:
        self.recoveries = 0

    async def recover_submission_outbox(self) -> None:
        self.recoveries += 1


class _FakeLease:
    def __init__(self, start_result: bool) -> None:
        self._start_result = start_result
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> bool:
        self.start_calls += 1
        return self._start_result

    async def stop(self) -> None:
        self.stop_calls += 1


def _live_safe_settings(**overrides):
    values = {
        "enable_multi_hub": True,
        "use_hub_router": True,
        "eod_exit_time": "15:20",
        "eod_exit_retry_cutoff_time": "15:30",
        "hub_subscription_poll_interval": 60.0,
        "enable_capital_checks": True,
        "capital_fail_closed_on_missing_state": True,
        "capital_fail_closed_on_missing_notional_price": True,
        "enable_risk_checks": True,
        "risk_enable_daily_loss": True,
        "risk_max_daily_loss": 1000.0,
        "enable_profit_checks": True,
        "profit_enable_daily_target": True,
        "profit_daily_target": 1000.0,
        "order_router_enforce_idempotency": True,
        "position_ownership_enabled": True,
        "position_ownership_unknown_mode": "block_entries",
        "order_lifecycle_persist_markers_required": True,
        "risk_fail_open_on_missing_pnl": False,
        "control_plane_backend": "postgres",
        "sweep_state_backend": "postgres",
        "broker_secret_backend": "secret_manager",
        "admin_api_key": "test-admin",
        "dashboard_hmac_auth_enabled": False,
        "dashboard_hmac_secret": None,
        "app_runtime_startup_validate": False,
        "schema_check_mode": "warn",
        "circuit_breaker_persist_state": True,
        "eod_cancel_open_orders_enabled": True,
        "ownership_persist_pending_locks": True,
        "hub_instance_name": "phoenix-live-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _set_live_safe_env(monkeypatch) -> None:
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.setenv("DISABLE_TRADING_WINDOW_FILTER", "false")
    monkeypatch.setenv("CLIENT_LOCAL_IP", "10.0.0.10")
    monkeypatch.setenv("CLIENT_PUBLIC_IP", "203.0.113.10")
    monkeypatch.setenv("MAC_ADDRESS", "02:00:00:00:00:20")
    monkeypatch.setenv("ORDER_SUBMISSION_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("ORDER_SUBMISSION_OUTBOX_REQUIRED", "true")
    monkeypatch.setenv("ORDER_SUBMISSION_OUTBOX_BACKEND", "postgres")


@pytest.fixture(autouse=True)
def _default_trade_mode(monkeypatch):
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    for key in (
        "EMA20_QTY",
        "EMA20_LOTS",
        "NIFTY_EMA20_QTY",
        "NIFTY_EMA20_LOTS",
        "BANKNIFTY_EMA20_QTY",
        "BANKNIFTY_EMA20_LOTS",
        "FINNIFTY_EMA20_QTY",
        "FINNIFTY_EMA20_LOTS",
        "MIDCPNIFTY_EMA20_QTY",
        "MIDCPNIFTY_EMA20_LOTS",
        "SENSEX_EMA20_QTY",
        "SENSEX_EMA20_LOTS",
        "NG_EMA20_QTY",
        "NG_EMA20_LOTS",
        "ALLOW_LEGACY_EMA20_QTY_IN_LIVE",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.anyio
async def test_app_runtime_start_stop_runs_worker_watchdog_and_hub(monkeypatch):
    monkeypatch.setenv("DISABLE_STREAM_WORKER", "0")
    monkeypatch.delenv("LEADER_LEASE_ENABLED", raising=False)
    hub = _DummyHub()
    runtime = AppRuntime(
        settings_getter=lambda: SimpleNamespace(enable_multi_hub=True),
        hub_runtime_getter=lambda: SimpleNamespace(hub=hub),
    )
    _attach_dummy_workers(runtime)
    worker = runtime.stream_worker
    watchdog = runtime.watchdog
    alert_evaluator = runtime.alert_evaluator

    await runtime.start()

    assert worker.started == 1
    assert watchdog.started == 1
    assert alert_evaluator.started == 1
    assert runtime.stream_worker_running() is True
    assert runtime.watchdog_running() is True
    assert hub.initialized == 1
    assert hub.started == 1

    await runtime.stop()

    assert watchdog.stopped == 1
    assert worker.stopped == 1
    assert alert_evaluator.stopped == 1
    assert hub.stopped == 1


@pytest.mark.anyio
async def test_app_runtime_waits_for_runner_startup_before_outbox_recovery(monkeypatch):
    monkeypatch.setenv("DISABLE_STREAM_WORKER", "0")
    monkeypatch.delenv("LEADER_LEASE_ENABLED", raising=False)
    hub = _DummyHub()
    lifecycle = _DummyLifecycle()
    order_router = _DummyOrderRouter()
    runtime_obj = SimpleNamespace(
        hub=hub,
        order_lifecycle=lifecycle,
        order_router=order_router,
    )
    runtime = AppRuntime(
        settings_getter=lambda: SimpleNamespace(enable_multi_hub=True),
        hub_runtime_getter=lambda: runtime_obj,
    )
    _attach_dummy_workers(runtime)

    await runtime.start()

    assert hub.initialized == 1
    assert hub.started == 1
    assert hub.startup_waits == 1
    assert lifecycle.started == 1
    assert order_router.recoveries == 1

    await runtime.stop()

    assert lifecycle.stopped == 1
    assert hub.stopped == 1


@pytest.mark.anyio
async def test_app_runtime_respects_disable_worker(monkeypatch):
    monkeypatch.setenv("DISABLE_STREAM_WORKER", "1")
    monkeypatch.delenv("LEADER_LEASE_ENABLED", raising=False)
    hub = _DummyHub()
    runtime = AppRuntime(
        settings_getter=lambda: SimpleNamespace(enable_multi_hub=False),
        hub_runtime_getter=lambda: SimpleNamespace(hub=hub),
    )
    _attach_dummy_workers(runtime)
    worker = runtime.stream_worker
    watchdog = runtime.watchdog
    alert_evaluator = runtime.alert_evaluator

    await runtime.start()

    assert worker.started == 0
    assert watchdog.started == 0
    assert alert_evaluator.started == 1
    assert hub.initialized == 0
    assert hub.started == 0

    await runtime.stop()
    assert worker.stopped == 1
    assert watchdog.stopped == 1
    assert alert_evaluator.stopped == 1
    assert hub.stopped == 0


@pytest.mark.anyio
async def test_app_runtime_skips_worker_and_hub_when_not_leader(monkeypatch):
    monkeypatch.setenv("DISABLE_STREAM_WORKER", "0")
    monkeypatch.setenv("LEADER_LEASE_ENABLED", "1")
    hub = _DummyHub()
    created_leases: list[_FakeLease] = []

    def _lease_factory(**_: object) -> _FakeLease:
        lease = _FakeLease(start_result=False)
        created_leases.append(lease)
        return lease

    runtime = AppRuntime(
        settings_getter=lambda: SimpleNamespace(enable_multi_hub=True),
        hub_runtime_getter=lambda: SimpleNamespace(hub=hub),
        leader_lease_factory=_lease_factory,
    )
    _attach_dummy_workers(runtime)
    worker = runtime.stream_worker
    watchdog = runtime.watchdog
    alert_evaluator = runtime.alert_evaluator

    await runtime.start()

    assert len(created_leases) == 1
    assert created_leases[0].start_calls == 1
    assert worker.started == 0
    assert watchdog.started == 0
    assert alert_evaluator.started == 0
    assert hub.initialized == 0
    assert hub.started == 0

    await runtime.stop()

    assert created_leases[0].stop_calls == 1
    assert worker.stopped == 1
    assert watchdog.stopped == 1
    assert alert_evaluator.stopped == 1


@pytest.mark.anyio
async def test_app_runtime_raises_on_hub_init_error_when_marker_strict_enabled(monkeypatch):
    monkeypatch.setenv("DISABLE_STREAM_WORKER", "1")
    monkeypatch.delenv("LEADER_LEASE_ENABLED", raising=False)

    def _failing_hub_runtime():
        raise RuntimeError("durable marker backend unavailable")

    runtime = AppRuntime(
        settings_getter=lambda: SimpleNamespace(
            enable_multi_hub=True,
            order_lifecycle_persist_markers_required=True,
        ),
        hub_runtime_getter=_failing_hub_runtime,
    )
    _attach_dummy_workers(runtime)

    with pytest.raises(RuntimeError, match="durable marker backend unavailable"):
        await runtime.start()


@pytest.mark.anyio
async def test_app_runtime_rejects_live_when_daily_loss_guard_disabled(monkeypatch):
    monkeypatch.setenv("DISABLE_STREAM_WORKER", "0")
    _set_live_safe_env(monkeypatch)
    monkeypatch.delenv("LEADER_LEASE_ENABLED", raising=False)
    monkeypatch.setattr(
        app_runtime,
        "check_startup_schema",
        lambda **_: SimpleNamespace(missing_tables=(), missing_indexes=()),
    )
    runtime = AppRuntime(
        settings_getter=lambda: _live_safe_settings(
            risk_enable_daily_loss=False,
        ),
        hub_runtime_getter=lambda: SimpleNamespace(hub=_DummyHub()),
    )
    _attach_dummy_workers(runtime)

    with pytest.raises(
        ValueError,
        match="TRADE_MODE=LIVE requires RISK_ENABLE_DAILY_LOSS=true",
    ):
        await runtime.start()

    await runtime.stop()


@pytest.mark.anyio
async def test_app_runtime_forces_strict_schema_check_in_live(monkeypatch):
    monkeypatch.setenv("DISABLE_STREAM_WORKER", "0")
    _set_live_safe_env(monkeypatch)
    monkeypatch.delenv("LEADER_LEASE_ENABLED", raising=False)
    observed: dict[str, str] = {}

    def _capture_schema_mode(**kwargs):
        observed["mode"] = str(kwargs.get("mode"))
        return SimpleNamespace(missing_tables=(), missing_indexes=())

    monkeypatch.setattr(app_runtime, "check_startup_schema", _capture_schema_mode)
    runtime = AppRuntime(
        settings_getter=lambda: _live_safe_settings(),
        hub_runtime_getter=lambda: SimpleNamespace(hub=_DummyHub()),
    )
    _attach_dummy_workers(runtime)

    await runtime.start()

    assert observed["mode"] == "strict"
    await runtime.stop()


@pytest.mark.anyio
async def test_app_runtime_records_operating_mode_status(monkeypatch):
    monkeypatch.setenv("DISABLE_STREAM_WORKER", "0")
    _set_live_safe_env(monkeypatch)
    monkeypatch.delenv("LEADER_LEASE_ENABLED", raising=False)
    monkeypatch.setattr(
        app_runtime,
        "check_startup_schema",
        lambda **_: SimpleNamespace(missing_tables=(), missing_indexes=()),
    )
    runtime = AppRuntime(
        settings_getter=lambda: _live_safe_settings(),
        hub_runtime_getter=lambda: SimpleNamespace(hub=_DummyHub()),
    )
    _attach_dummy_workers(runtime)

    await runtime.start()

    assert runtime.operating_mode_status()["mode"] == "HUB_AUTHORITATIVE"
    assert runtime.operating_mode_status()["authority_path"] == "hub"
    await runtime.stop()


@pytest.mark.anyio
async def test_app_runtime_startup_validation_rejects_invalid_intervals(monkeypatch):
    monkeypatch.setenv("DISABLE_STREAM_WORKER", "1")
    monkeypatch.setenv("POSITION_SYNC_INTERVAL_SECONDS", "0")
    monkeypatch.delenv("LEADER_LEASE_ENABLED", raising=False)
    runtime = AppRuntime(
        settings_getter=lambda: SimpleNamespace(
            enable_multi_hub=False,
            risk_enable_daily_loss=False,
            app_runtime_startup_validate=True,
            eod_exit_time="15:20",
            eod_exit_retry_cutoff_time="15:30",
            hub_subscription_poll_interval=60.0,
            control_plane_backend="firestore",
            sweep_state_backend="firestore",
            schema_check_mode="warn",
        ),
        hub_runtime_getter=lambda: SimpleNamespace(hub=_DummyHub()),
    )
    _attach_dummy_workers(runtime)

    with pytest.raises(ValueError, match="POSITION_SYNC_INTERVAL_SECONDS"):
        await runtime.start()


def test_stream_worker_marks_non_retryable_error_and_blocks_restart(monkeypatch):
    call_count = {"count": 0}

    def _raise_non_retryable(**_: object) -> None:
        call_count["count"] += 1
        raise ValueError("ANGEL_TOTP_SECRET environment variable not set")

    monkeypatch.setitem(
        sys.modules,
        "app.runners.multi_instrument_stream",
        SimpleNamespace(stream_multi_instruments=_raise_non_retryable),
    )
    worker = StreamWorker(StrategySwitchboard(), InstrumentController())

    worker.start()
    deadline = time.time() + 1.0
    while worker.running() and time.time() < deadline:
        time.sleep(0.01)

    fatal_error = worker.fatal_error()
    assert fatal_error is not None
    assert "ANGEL_TOTP_SECRET environment variable not set" in fatal_error
    assert call_count["count"] == 1

    worker.start()
    time.sleep(0.05)
    assert call_count["count"] == 1
    worker.stop()


def test_stream_worker_treats_missing_required_attachments_as_non_retryable(monkeypatch):
    call_count = {"count": 0}

    def _raise_missing_routes(**_: object) -> None:
        call_count["count"] += 1
        raise RuntimeError(
            "Required runtime strategy attachment(s) missing after startup validation: "
            "BANKNIFTY_IDX:ce_orb, NIFTY_IDX:ce_orb"
        )

    monkeypatch.setitem(
        sys.modules,
        "app.runners.multi_instrument_stream",
        SimpleNamespace(stream_multi_instruments=_raise_missing_routes),
    )
    worker = StreamWorker(StrategySwitchboard(), InstrumentController())

    worker.start()
    deadline = time.time() + 1.0
    while worker.running() and time.time() < deadline:
        time.sleep(0.01)

    fatal_error = worker.fatal_error()
    assert fatal_error is not None
    assert "Required runtime strategy attachment(s) missing after startup validation" in fatal_error
    assert call_count["count"] == 1

    worker.start()
    time.sleep(0.05)
    assert call_count["count"] == 1
    worker.stop()


def test_worker_watchdog_suppresses_restart_for_fatal_worker():
    class _FatalWorker:
        def __init__(self) -> None:
            self.start_calls = 0

        def running(self) -> bool:
            return False

        def fatal_error(self) -> str:
            return "BROKER_SECRET_BACKEND=postgres missing control-plane settings"

        def start(self) -> None:
            self.start_calls += 1

    class _StopEvent:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, _: float) -> bool:
            self.calls += 1
            return self.calls > 1

        def set(self) -> None:
            return

    worker = _FatalWorker()
    watchdog = WorkerWatchdog(worker, interval_seconds=0.01)
    watchdog._stop_event = _StopEvent()  # type: ignore[assignment]

    watchdog._run()
    assert worker.start_calls == 0


def test_worker_watchdog_applies_restart_backoff():
    now = {"t": 0.0}

    class _Worker:
        def __init__(self) -> None:
            self.start_calls = 0

        def running(self) -> bool:
            return False

        def fatal_error(self) -> None:
            return None

        def start(self) -> None:
            self.start_calls += 1

    class _StopEvent:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, _: float) -> bool:
            self.calls += 1
            now["t"] += 1.0
            return self.calls > 5

        def set(self) -> None:
            return

    worker = _Worker()
    watchdog = WorkerWatchdog(
        worker,
        interval_seconds=0.01,
        restart_backoff_base_seconds=2.0,
        restart_backoff_max_seconds=8.0,
        restart_backoff_jitter_ratio=0.0,
        stable_run_window_seconds=0.0,
        now_mono=lambda: float(now["t"]),
        jitter_fn=lambda: 0.0,
    )
    watchdog._stop_event = _StopEvent()  # type: ignore[assignment]

    watchdog._run()
    snapshot = watchdog.status_snapshot()
    assert worker.start_calls == 2
    assert snapshot["restart_attempts"] == 2
    assert snapshot["current_backoff_seconds"] == 4.0
    assert snapshot["next_restart_in_seconds"] == 1.0


def test_worker_watchdog_resets_backoff_after_stable_run():
    now = {"t": 0.0}

    class _Worker:
        def running(self) -> bool:
            return True

        def fatal_error(self) -> None:
            return None

        def start(self) -> None:
            return

    class _StopEvent:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, _: float) -> bool:
            self.calls += 1
            now["t"] += 5.0
            return self.calls > 2

        def set(self) -> None:
            return

    watchdog = WorkerWatchdog(
        _Worker(),
        interval_seconds=0.01,
        stable_run_window_seconds=3.0,
        now_mono=lambda: float(now["t"]),
        jitter_fn=lambda: 0.0,
    )
    watchdog._restart_attempts = 3
    watchdog._current_backoff_seconds = 8.0
    watchdog._next_restart_ts = 100.0
    watchdog._stop_event = _StopEvent()  # type: ignore[assignment]

    watchdog._run()
    snapshot = watchdog.status_snapshot()
    assert snapshot["restart_attempts"] == 0
    assert snapshot["current_backoff_seconds"] == 0.0
    assert snapshot["next_restart_in_seconds"] == 0.0
