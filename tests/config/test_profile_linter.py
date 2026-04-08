from __future__ import annotations

from pathlib import Path

from app.config.profile_linter import lint_env_profile


def test_shared_repo_profiles_pass_lint():
    repo_root = Path(__file__).resolve().parents[2]
    profile_paths = [
        repo_root / ".env.example",
        repo_root / "docker.env",
        repo_root / "docs" / "runbooks" / "docker-live.env.example",
        repo_root / "docs" / "runbooks" / "cloudrun-live.env.example",
    ]

    for path in profile_paths:
        assert lint_env_profile(path) == [], f"{path.name} lint errors: {lint_env_profile(path)}"


def test_linter_rejects_env_secret_backend_in_live(tmp_path):
    """LIVE rejects env as a broker secret source."""
    profile_path = tmp_path / "env-secrets-live.env"
    profile_path.write_text(
        "APP_ENV=production\n"
        "TRADE_MODE=LIVE\n"
        "BROKER_SECRET_BACKEND=env\n"
        "ADMIN_API_KEY=test-admin\n"
        "CONTROL_PLANE_BACKEND=postgres\n"
        "SWEEP_STATE_BACKEND=postgres\n"
        "POSITION_SYNC_INTERVAL_SECONDS=30\n"
        "ORDERS_SYNC_INTERVAL_SECONDS=90\n"
        "APP_RUNTIME_STARTUP_VALIDATE=true\n"
        "SCHEMA_CHECK_MODE=strict\n",
        encoding="utf-8",
    )

    errors = lint_env_profile(profile_path)

    secret_errors = [e for e in errors if "BROKER_SECRET_BACKEND" in e]
    assert secret_errors, "env backend should be rejected in LIVE"


def test_linter_accepts_postgres_secret_backend_in_live(tmp_path):
    """LIVE accepts postgres as broker secret backend."""
    profile_path = tmp_path / "postgres-secrets-live.env"
    profile_path.write_text(
        "APP_ENV=production\n"
        "TRADE_MODE=LIVE\n"
        "BROKER_SECRET_BACKEND=postgres\n"
        "ADMIN_API_KEY=test-admin\n"
        "CONTROL_PLANE_BACKEND=postgres\n"
        "SWEEP_STATE_BACKEND=postgres\n"
        "POSITION_SYNC_INTERVAL_SECONDS=30\n"
        "ORDERS_SYNC_INTERVAL_SECONDS=90\n"
        "APP_RUNTIME_STARTUP_VALIDATE=true\n"
        "SCHEMA_CHECK_MODE=strict\n",
        encoding="utf-8",
    )

    errors = lint_env_profile(profile_path)

    secret_errors = [e for e in errors if "BROKER_SECRET_BACKEND" in e]
    assert secret_errors == [], f"postgres backend should be accepted in LIVE: {secret_errors}"


def test_linter_rejects_unknown_secret_backend_in_live(tmp_path):
    """LIVE rejects unrecognized broker secret backends."""
    profile_path = tmp_path / "bad-secrets-live.env"
    profile_path.write_text(
        "APP_ENV=production\n"
        "TRADE_MODE=LIVE\n"
        "BROKER_SECRET_BACKEND=plaintext_file\n"
        "ADMIN_API_KEY=test-admin\n"
        "CONTROL_PLANE_BACKEND=postgres\n"
        "SWEEP_STATE_BACKEND=postgres\n"
        "POSITION_SYNC_INTERVAL_SECONDS=30\n"
        "ORDERS_SYNC_INTERVAL_SECONDS=90\n"
        "APP_RUNTIME_STARTUP_VALIDATE=true\n"
        "SCHEMA_CHECK_MODE=strict\n",
        encoding="utf-8",
    )

    errors = lint_env_profile(profile_path)

    assert any("BROKER_SECRET_BACKEND" in e for e in errors)


def test_linter_accepts_secret_manager_backend_in_live(tmp_path):
    """LIVE accepts secret_manager as broker secret backend (Cloud Run path)."""
    profile_path = tmp_path / "sm-secrets-live.env"
    profile_path.write_text(
        "APP_ENV=production\n"
        "TRADE_MODE=LIVE\n"
        "BROKER_SECRET_BACKEND=secret_manager\n"
        "GCP_PROJECT_ID=test-project\n"
        "ADMIN_API_KEY=test-admin\n"
        "CONTROL_PLANE_BACKEND=postgres\n"
        "SWEEP_STATE_BACKEND=postgres\n"
        "POSITION_SYNC_INTERVAL_SECONDS=30\n"
        "ORDERS_SYNC_INTERVAL_SECONDS=90\n"
        "APP_RUNTIME_STARTUP_VALIDATE=true\n"
        "SCHEMA_CHECK_MODE=strict\n",
        encoding="utf-8",
    )

    errors = lint_env_profile(profile_path)

    secret_errors = [e for e in errors if "BROKER_SECRET_BACKEND" in e]
    assert secret_errors == [], f"secret_manager backend should be accepted in LIVE: {secret_errors}"


def test_linter_rejects_live_profile_missing_broker_network_identity(tmp_path):
    profile_path = tmp_path / "missing-network-live.env"
    profile_path.write_text(
        "APP_ENV=production\n"
        "TRADE_MODE=LIVE\n"
        "BROKER_SECRET_BACKEND=postgres\n"
        "ADMIN_API_KEY=test-admin\n"
        "CONTROL_PLANE_BACKEND=postgres\n"
        "SWEEP_STATE_BACKEND=postgres\n"
        "ENABLE_CAPITAL_CHECKS=true\n"
        "CAPITAL_FAIL_CLOSED_ON_MISSING_STATE=true\n"
        "CAPITAL_FAIL_CLOSED_ON_MISSING_NOTIONAL_PRICE=true\n"
        "ENABLE_RISK_CHECKS=true\n"
        "RISK_ENABLE_DAILY_LOSS=true\n"
        "RISK_MAX_DAILY_LOSS=2000\n"
        "RISK_FAIL_OPEN_ON_MISSING_PNL=false\n"
        "ENABLE_PROFIT_CHECKS=true\n"
        "PROFIT_ENABLE_DAILY_TARGET=true\n"
        "PROFIT_DAILY_TARGET=2000\n"
        "ORDER_ROUTER_ENFORCE_IDEMPOTENCY=true\n"
        "POSITION_OWNERSHIP_ENABLED=true\n"
        "POSITION_OWNERSHIP_UNKNOWN_MODE=block_entries\n"
        "ORDER_LIFECYCLE_PERSIST_MARKERS_REQUIRED=true\n"
        "ENABLE_MULTI_HUB=true\n"
        "USE_HUB_ROUTER=true\n"
        "DISABLE_STREAM_WORKER=false\n"
        "ORDER_SUBMISSION_OUTBOX_ENABLED=true\n"
        "ORDER_SUBMISSION_OUTBOX_REQUIRED=true\n"
        "ORDER_SUBMISSION_OUTBOX_BACKEND=postgres\n"
        "EOD_EXIT_TIME=15:20\n"
        "EOD_CANCEL_OPEN_ORDERS_ENABLED=true\n"
        "CIRCUIT_BREAKER_PERSIST_STATE=true\n"
        "POSITION_SYNC_INTERVAL_SECONDS=30\n"
        "ORDERS_SYNC_INTERVAL_SECONDS=90\n"
        "APP_RUNTIME_STARTUP_VALIDATE=true\n"
        "SCHEMA_CHECK_MODE=strict\n",
        encoding="utf-8",
    )

    errors = lint_env_profile(profile_path)

    assert "CLIENT_LOCAL_IP must be set in TRADE_MODE=LIVE" in errors
    assert "CLIENT_PUBLIC_IP must be set in TRADE_MODE=LIVE" in errors
    assert "MAC_ADDRESS must be set in TRADE_MODE=LIVE" in errors


def test_cloudrun_profile_passes_lint():
    """The committed Cloud Run profile passes all lint checks."""
    repo_root = Path(__file__).resolve().parents[2]
    profile_path = repo_root / "docs" / "runbooks" / "cloudrun-live.env.example"
    errors = lint_env_profile(profile_path)
    assert errors == [], f"cloudrun-live.env.example lint errors: {errors}"


def test_linter_rejects_live_profile_with_relaxed_safety(tmp_path):
    profile_path = tmp_path / "unsafe-live.env"
    profile_path.write_text(
        "APP_ENV=production\n"
        "TRADE_MODE=LIVE\n"
        "DASHBOARD_AUTH_DISABLED=true\n"
        "ADMIN_API_KEY=test-admin\n"
        "CONTROL_PLANE_BACKEND=postgres\n"
        "SWEEP_STATE_BACKEND=postgres\n"
        "ENABLE_CAPITAL_CHECKS=true\n"
        "ENABLE_RISK_CHECKS=true\n"
        "RISK_ENABLE_DAILY_LOSS=true\n"
        "RISK_MAX_DAILY_LOSS=2000\n"
        "RISK_FAIL_OPEN_ON_MISSING_PNL=true\n"
        "ENABLE_PROFIT_CHECKS=true\n"
        "PROFIT_ENABLE_DAILY_TARGET=true\n"
        "PROFIT_DAILY_TARGET=2000\n"
        "POSITION_SYNC_INTERVAL_SECONDS=30\n"
        "ORDERS_SYNC_INTERVAL_SECONDS=90\n"
        "APP_RUNTIME_STARTUP_VALIDATE=false\n"
        "SCHEMA_CHECK_MODE=warn\n",
        encoding="utf-8",
    )

    errors = lint_env_profile(profile_path)

    assert "non-local profiles require APP_RUNTIME_STARTUP_VALIDATE=true" in errors
    assert "non-local profiles require SCHEMA_CHECK_MODE=strict" in errors
    assert "DASHBOARD_AUTH_DISABLED must be false outside local/dev/test" in errors
    assert "TRADE_MODE=LIVE requires RISK_FAIL_OPEN_ON_MISSING_PNL=false" in errors


def test_linter_rejects_non_local_mock_sweep_state_escape_hatch(tmp_path):
    profile_path = tmp_path / "unsafe-production-paper.env"
    profile_path.write_text(
        "APP_ENV=production\n"
        "TRADE_MODE=PAPER\n"
        "ALLOW_MOCK_SWEEP_STATE=true\n"
        "DASHBOARD_AUTH_DISABLED=false\n"
        "ADMIN_API_KEY=test-admin\n"
        "POSITION_SYNC_INTERVAL_SECONDS=30\n"
        "ORDERS_SYNC_INTERVAL_SECONDS=90\n"
        "APP_RUNTIME_STARTUP_VALIDATE=true\n"
        "SCHEMA_CHECK_MODE=strict\n",
        encoding="utf-8",
    )

    errors = lint_env_profile(profile_path)

    assert (
        "APP_ENV=production forbids enabling mock sweep/EOD state escape hatches: "
        "ALLOW_MOCK_SWEEP_STATE"
    ) in errors


def test_linter_rejects_live_mock_sweep_state_escape_hatch(tmp_path):
    profile_path = tmp_path / "unsafe-live-mock-sweep.env"
    profile_path.write_text(
        "APP_ENV=production\n"
        "TRADE_MODE=LIVE\n"
        "ALLOW_MOCK_SWEEP_STATE=true\n"
        "DASHBOARD_AUTH_DISABLED=false\n"
        "ADMIN_API_KEY=test-admin\n"
        "CONTROL_PLANE_BACKEND=postgres\n"
        "SWEEP_STATE_BACKEND=postgres\n"
        "BROKER_SECRET_BACKEND=secret_manager\n"
        "ENABLE_CAPITAL_CHECKS=true\n"
        "CAPITAL_FAIL_CLOSED_ON_MISSING_STATE=true\n"
        "CAPITAL_FAIL_CLOSED_ON_MISSING_NOTIONAL_PRICE=true\n"
        "ENABLE_RISK_CHECKS=true\n"
        "RISK_ENABLE_DAILY_LOSS=true\n"
        "RISK_MAX_DAILY_LOSS=2000\n"
        "RISK_FAIL_OPEN_ON_MISSING_PNL=false\n"
        "ENABLE_PROFIT_CHECKS=true\n"
        "PROFIT_ENABLE_DAILY_TARGET=true\n"
        "PROFIT_DAILY_TARGET=2000\n"
        "ORDER_ROUTER_ENFORCE_IDEMPOTENCY=true\n"
        "POSITION_OWNERSHIP_ENABLED=true\n"
        "POSITION_OWNERSHIP_UNKNOWN_MODE=block_entries\n"
        "ORDER_LIFECYCLE_PERSIST_MARKERS_REQUIRED=true\n"
        "ENABLE_MULTI_HUB=true\n"
        "USE_HUB_ROUTER=true\n"
        "DISABLE_STREAM_WORKER=false\n"
        "ORDER_SUBMISSION_OUTBOX_ENABLED=true\n"
        "ORDER_SUBMISSION_OUTBOX_REQUIRED=true\n"
        "ORDER_SUBMISSION_OUTBOX_BACKEND=postgres\n"
        "EOD_EXIT_TIME=15:20\n"
        "EOD_CANCEL_OPEN_ORDERS_ENABLED=true\n"
        "CIRCUIT_BREAKER_PERSIST_STATE=true\n"
        "POSITION_SYNC_INTERVAL_SECONDS=30\n"
        "ORDERS_SYNC_INTERVAL_SECONDS=90\n"
        "APP_RUNTIME_STARTUP_VALIDATE=true\n"
        "SCHEMA_CHECK_MODE=strict\n",
        encoding="utf-8",
    )

    errors = lint_env_profile(profile_path)

    assert (
        "TRADE_MODE=LIVE forbids mock sweep/EOD state escape hatches: "
        "ALLOW_MOCK_SWEEP_STATE"
    ) in errors


def test_linter_rejects_break_glass_in_committed_profile(tmp_path):
    profile_path = tmp_path / "break-glass-live.env"
    profile_path.write_text(
        "APP_ENV=production\n"
        "TRADE_MODE=LIVE\n"
        "DASHBOARD_AUTH_DISABLED=false\n"
        "ADMIN_API_KEY=test-admin\n"
        "CONTROL_PLANE_BACKEND=postgres\n"
        "SWEEP_STATE_BACKEND=postgres\n"
        "ENABLE_CAPITAL_CHECKS=true\n"
        "CAPITAL_FAIL_CLOSED_ON_MISSING_STATE=true\n"
        "CAPITAL_FAIL_CLOSED_ON_MISSING_NOTIONAL_PRICE=true\n"
        "ENABLE_RISK_CHECKS=true\n"
        "RISK_ENABLE_DAILY_LOSS=true\n"
        "RISK_MAX_DAILY_LOSS=2000\n"
        "RISK_FAIL_OPEN_ON_MISSING_PNL=false\n"
        "ENABLE_PROFIT_CHECKS=true\n"
        "PROFIT_ENABLE_DAILY_TARGET=true\n"
        "PROFIT_DAILY_TARGET=2000\n"
        "ORDER_ROUTER_ENFORCE_IDEMPOTENCY=true\n"
        "POSITION_OWNERSHIP_ENABLED=true\n"
        "POSITION_OWNERSHIP_UNKNOWN_MODE=block_entries\n"
        "ORDER_LIFECYCLE_PERSIST_MARKERS_REQUIRED=true\n"
        "POSITION_SYNC_INTERVAL_SECONDS=30\n"
        "ORDERS_SYNC_INTERVAL_SECONDS=90\n"
        "APP_RUNTIME_STARTUP_VALIDATE=true\n"
        "SCHEMA_CHECK_MODE=strict\n"
        "LIVE_RUNTIME_OVERRIDE_BREAK_GLASS=true\n"
        "LIVE_RUNTIME_OVERRIDE_BREAK_GLASS_APPROVED_BY=ops-oncall\n"
        "LIVE_RUNTIME_OVERRIDE_BREAK_GLASS_UNTIL=2099-01-01T00:00:00Z\n",
        encoding="utf-8",
    )

    errors = lint_env_profile(profile_path)

    assert (
        "LIVE_RUNTIME_OVERRIDE_BREAK_GLASS must be false in committed env profiles"
        in errors
    )
    assert (
        "LIVE_RUNTIME_OVERRIDE_BREAK_GLASS_APPROVED_BY must be unset in committed env profiles"
        in errors
    )
    assert (
        "LIVE_RUNTIME_OVERRIDE_BREAK_GLASS_UNTIL must be unset in committed env profiles"
        in errors
    )
