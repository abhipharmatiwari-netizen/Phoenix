from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.data.oi_snapshotter_runtime import (
    OiSnapshotterRuntimeConfig,
    clear_cached_angel_quote_client,
    load_runtime_config,
    run_runtime,
)
from app.data.oi_snapshotter import SnapshotResult


@pytest.fixture(autouse=True)
def _clear_angel_quote_cache():
    clear_cached_angel_quote_client()
    yield
    clear_cached_angel_quote_client()


def test_runtime_config_defaults_to_disabled_and_requires_explicit_expiry():
    config = load_runtime_config(env={"OI_SNAPSHOTTER_EXPIRY": "2026-05-19"})

    assert config.enabled is False
    assert config.provider == "angel"
    assert config.underlying == "NIFTY"
    assert config.expiry == date(2026, 5, 19)
    assert config.reuse_broker_session is True
    assert config.session_max_age_minutes == 600
    assert config.nse_validation_enabled is False


def test_runtime_config_reads_nse_validation_settings():
    config = load_runtime_config(
        env={
            "OI_SNAPSHOTTER_EXPIRY": "2026-05-19",
            "OI_ML_ENABLE_NSE_VALIDATION": "true",
            "OI_ML_NSE_VALIDATION_STORE_QUOTES": "false",
            "OI_ML_NSE_VALIDATION_LOG_ALL": "false",
            "OI_ML_NSE_VALIDATION_FAIL_ON_ERROR": "true",
            "OI_ML_NSE_VALIDATION_TIMEOUT_SECONDS": "7",
            "OI_ML_NSE_VALIDATION_MAX_ATTEMPTS": "4",
            "OI_ML_NSE_VALIDATION_RETRY_BACKOFF_SECONDS": "0.2",
            "OI_ML_NSE_VALIDATION_RETRY_JITTER_SECONDS": "0.05",
            "OI_ML_NSE_VALIDATION_ERROR_RATE_WINDOW": "12",
            "OI_ML_NSE_VALIDATION_ERROR_RATE_WARN_THRESHOLD": "0.4",
            "OI_ML_NSE_VALIDATION_VOLUME_ABS_TOLERANCE": "100",
            "OI_ML_NSE_VALIDATION_PRICE_PCT_TOLERANCE": "0.02",
        }
    )

    assert config.nse_validation_enabled is True
    assert config.nse_validation_store_quotes is False
    assert config.nse_validation_log_all is False
    assert config.nse_validation_fail_on_error is True
    assert config.nse_validation_timeout_seconds == 7
    assert config.nse_validation_max_attempts == 4
    assert config.nse_validation_retry_backoff_seconds == 0.2
    assert config.nse_validation_retry_jitter_seconds == 0.05
    assert config.nse_validation_error_rate_window == 12
    assert config.nse_validation_error_rate_warn_threshold == 0.4
    assert config.nse_validation_volume_abs_tolerance == 100
    assert config.nse_validation_price_pct_tolerance == 0.02


def test_runtime_config_reads_session_reuse_settings():
    config = load_runtime_config(
        env={
            "OI_SNAPSHOTTER_EXPIRY": "2026-05-19",
            "OI_SNAPSHOTTER_REUSE_BROKER_SESSION": "false",
            "OI_SNAPSHOTTER_SESSION_MAX_AGE_MINUTES": "30",
        }
    )

    assert config.reuse_broker_session is False
    assert config.session_max_age_minutes == 30


def test_runtime_config_rejects_missing_expiry_even_when_disabled():
    with pytest.raises(ValueError, match="requires --expiry"):
        load_runtime_config(env={})


def test_run_runtime_disabled_is_noop_without_login(monkeypatch):
    def _boom():
        raise AssertionError("login should not be called when disabled")

    monkeypatch.setattr(
        "app.data.oi_snapshotter_runtime.angel_login.angel_login_and_get_tokens",
        _boom,
    )
    result = run_runtime(
        OiSnapshotterRuntimeConfig(
            enabled=False,
            provider="angel",
            underlying="NIFTY",
            expiry=date(2026, 5, 19),
        )
    )

    assert result == []


def test_run_runtime_rejects_unknown_provider_before_login(monkeypatch):
    def _boom():
        raise AssertionError("login should not be called for unsupported provider")

    monkeypatch.setattr(
        "app.data.oi_snapshotter_runtime.angel_login.angel_login_and_get_tokens",
        _boom,
    )
    with pytest.raises(ValueError, match="unsupported"):
        run_runtime(
            OiSnapshotterRuntimeConfig(
                enabled=True,
                provider="unknown",
                underlying="NIFTY",
                expiry=date(2026, 5, 19),
            )
        )


def test_run_runtime_reuses_angel_quote_session(monkeypatch):
    login_calls = []
    client_instances = []

    def fake_login():
        login_calls.append(1)
        return {
            "jwtToken": "jwt",
            "API_KEY": "api",
            "client_local_ip": "127.0.0.1",
            "client_public_ip": "127.0.0.1",
            "mac_address": "00:00:00:00:00:00",
        }

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            client_instances.append(self)

    class FakeProvider:
        def __init__(self, *, quote_fetcher, scrip_master, batch_size):
            self.quote_fetcher = quote_fetcher
            self.scrip_master = scrip_master
            self.batch_size = batch_size

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeStore:
        def __init__(self, conn, *, commit):
            self.conn = conn
            self.commit = commit

    class FakeSnapshotter:
        def __init__(self, *, provider, store):
            self.provider = provider
            self.store = store

        def capture_once(self, *, underlying, expiry):
            return SnapshotResult(
                provider="angel",
                underlying=underlying,
                expiry=expiry,
                snapshot_ts=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
                fetched_count=1,
                stored_count=1,
                unusable_for_live_count=0,
            )

    monkeypatch.setattr(
        "app.data.oi_snapshotter_runtime.angel_login.angel_login_and_get_tokens",
        fake_login,
    )
    monkeypatch.setattr("app.data.oi_snapshotter_runtime.AngelOrderClient", FakeClient)
    monkeypatch.setattr(
        "app.data.oi_snapshotter_runtime.AngelOptionChainProvider",
        FakeProvider,
    )
    monkeypatch.setattr("app.data.oi_snapshotter_runtime.load_scrip_master", lambda: [])
    monkeypatch.setattr(
        "app.data.oi_snapshotter_runtime.connect_with_retry",
        lambda *_, **__: FakeConn(),
    )
    monkeypatch.setattr("app.data.oi_snapshotter_runtime.get_control_plane_dsn", lambda: "dsn")
    monkeypatch.setattr("app.data.oi_snapshotter_runtime.OptionChainStore", FakeStore)
    monkeypatch.setattr("app.data.oi_snapshotter_runtime.OiSnapshotter", FakeSnapshotter)

    config = OiSnapshotterRuntimeConfig(
        enabled=True,
        provider="angel",
        underlying="NIFTY",
        expiry=date(2026, 5, 19),
        once=True,
    )

    assert run_runtime(config)[0].stored_count == 1
    assert run_runtime(config)[0].stored_count == 1

    assert len(login_calls) == 1
    assert len(client_instances) == 1


def test_run_runtime_wires_nse_validator_when_enabled(monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeProvider:
        def __init__(self, *, quote_fetcher, scrip_master, batch_size):
            self.quote_fetcher = quote_fetcher
            self.scrip_master = scrip_master
            self.batch_size = batch_size

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeStore:
        def __init__(self, conn, *, commit):
            self.conn = conn
            self.commit = commit

    class FakeSnapshotter:
        def __init__(self, *, provider, store, validator=None):
            self.validator = validator

        def capture_once(self, *, underlying, expiry):
            return SnapshotResult(
                provider="angel",
                underlying=underlying,
                expiry=expiry,
                snapshot_ts=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
                fetched_count=1,
                stored_count=1,
                unusable_for_live_count=0,
                validation_status=getattr(self.validator, "status", None),
            )

    monkeypatch.setattr(
        "app.data.oi_snapshotter_runtime.angel_login.angel_login_and_get_tokens",
        lambda: {"jwtToken": "jwt", "API_KEY": "api"},
    )
    monkeypatch.setattr("app.data.oi_snapshotter_runtime.AngelOrderClient", FakeClient)
    monkeypatch.setattr(
        "app.data.oi_snapshotter_runtime.AngelOptionChainProvider",
        FakeProvider,
    )
    monkeypatch.setattr("app.data.oi_snapshotter_runtime.load_scrip_master", lambda: [])
    monkeypatch.setattr(
        "app.data.oi_snapshotter_runtime.connect_with_retry",
        lambda *_, **__: FakeConn(),
    )
    monkeypatch.setattr("app.data.oi_snapshotter_runtime.get_control_plane_dsn", lambda: "dsn")
    monkeypatch.setattr("app.data.oi_snapshotter_runtime.OptionChainStore", FakeStore)
    monkeypatch.setattr("app.data.oi_snapshotter_runtime.OiSnapshotter", FakeSnapshotter)
    monkeypatch.setattr(
        "app.data.oi_snapshotter_runtime._build_nse_validator",
        lambda config, conn: type("Validator", (), {"status": "OK"})(),
    )

    result = run_runtime(
        OiSnapshotterRuntimeConfig(
            enabled=True,
            provider="angel",
            underlying="NIFTY",
            expiry=date(2026, 5, 19),
            once=True,
            nse_validation_enabled=True,
        )
    )[0]

    assert result.validation_status == "OK"
