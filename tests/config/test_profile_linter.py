from __future__ import annotations

from pathlib import Path

from app.config.profile_linter import lint_env_profile


def test_shared_repo_profiles_pass_lint():
    repo_root = Path(__file__).resolve().parents[2]
    profile_paths = [
        repo_root / ".env.example",
        repo_root / "docker.env",
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


def test_linter_rejects_redis_pnl_backend_in_live(tmp_path):
    """#119: TRADE_MODE=LIVE must reject PNL_STATE_BACKEND=redis."""
    profile_path = tmp_path / "redis-pnl-live.env"
    profile_path.write_text(
        "APP_ENV=production\n"
        "TRADE_MODE=LIVE\n"
        "PNL_STATE_BACKEND=redis\n"
        "ADMIN_API_KEY=test-admin\n"
        "CONTROL_PLANE_BACKEND=postgres\n"
        "SWEEP_STATE_BACKEND=postgres\n"
        "BROKER_SECRET_BACKEND=postgres\n"
        "ENABLE_CAPITAL_CHECKS=true\n"
        "ALLOW_LIVE_CAPITAL_LIMITS_DEFAULT_ONLY=true\n"
        "ENABLE_RISK_CHECKS=true\n"
        "RISK_ENABLE_DAILY_LOSS=true\n"
        "RISK_MAX_DAILY_LOSS=2000\n"
        "ENABLE_PROFIT_CHECKS=true\n"
        "PROFIT_ENABLE_DAILY_TARGET=true\n"
        "PROFIT_DAILY_TARGET=2000\n",
        encoding="utf-8",
    )

    errors = lint_env_profile(profile_path)
    assert any("PNL_STATE_BACKEND=redis" in e for e in errors), (
        f"Expected Redis PnL rejection but got: {errors}"
    )


def test_linter_accepts_postgres_pnl_backend_in_live(tmp_path):
    """#119: PNL_STATE_BACKEND=postgres must not produce an error in LIVE."""
    profile_path = tmp_path / "postgres-pnl-live.env"
    profile_path.write_text(
        "APP_ENV=production\n"
        "TRADE_MODE=LIVE\n"
        "PNL_STATE_BACKEND=postgres\n"
        "ADMIN_API_KEY=test-admin\n"
        "CONTROL_PLANE_BACKEND=postgres\n"
        "SWEEP_STATE_BACKEND=postgres\n"
        "BROKER_SECRET_BACKEND=postgres\n"
        "ENABLE_CAPITAL_CHECKS=true\n"
        "ALLOW_LIVE_CAPITAL_LIMITS_DEFAULT_ONLY=true\n"
        "ENABLE_RISK_CHECKS=true\n"
        "RISK_ENABLE_DAILY_LOSS=true\n"
        "RISK_MAX_DAILY_LOSS=2000\n"
        "ENABLE_PROFIT_CHECKS=true\n"
        "PROFIT_ENABLE_DAILY_TARGET=true\n"
        "PROFIT_DAILY_TARGET=2000\n",
        encoding="utf-8",
    )

    errors = lint_env_profile(profile_path)
    redis_errors = [e for e in errors if "PNL_STATE_BACKEND" in e]
    assert redis_errors == [], f"Unexpected PnL backend errors: {redis_errors}"


# ---------------------------------------------------------------------------
# HUB_DEFAULT_TENANT_ID (#193)
# ---------------------------------------------------------------------------

def test_profile_linter_parses_hub_default_tenant_id(tmp_path):
    """hub_default_tenant_id is parsed from HUB_DEFAULT_TENANT_ID env var."""
    from app.config.profile_linter import _build_lint_settings  # type: ignore[attr-defined]
    settings = _build_lint_settings({"HUB_DEFAULT_TENANT_ID": "my-tenant"})
    assert settings.hub_default_tenant_id == "my-tenant"


def test_profile_linter_hub_default_tenant_id_empty_string(tmp_path):
    """hub_default_tenant_id is empty string when env var is absent."""
    from app.config.profile_linter import _build_lint_settings  # type: ignore[attr-defined]
    settings = _build_lint_settings({})
    assert settings.hub_default_tenant_id == ""


def test_linter_rejects_live_profile_missing_hub_default_tenant_id(tmp_path):
    """LIVE profile missing HUB_DEFAULT_TENANT_ID must produce a validation error."""
    profile_path = tmp_path / "no-hub-tenant-live.env"
    profile_path.write_text(
        "APP_ENV=production\n"
        "TRADE_MODE=LIVE\n"
        "BROKER_SECRET_BACKEND=postgres\n"
        "ADMIN_API_KEY=test-admin\n"
        "CONTROL_PLANE_BACKEND=postgres\n"
        "SWEEP_STATE_BACKEND=postgres\n"
        "ENABLE_CAPITAL_CHECKS=true\n"
        "CAPITAL_LIMITS_JSON={\"A1\": {}}\n"
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
        "OWNERSHIP_PERSIST_PENDING_LOCKS=true\n"
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
        "HUB_INSTANCE_NAME=phoenix-live-1\n"
        "LEADER_LEASE_ENABLED=true\n"
        "LEADER_LEASE_BACKEND=postgres\n"
        "CLIENT_LOCAL_IP=10.0.0.1\n"
        "CLIENT_PUBLIC_IP=1.2.3.4\n"
        "MAC_ADDRESS=aa:bb:cc:dd:ee:ff\n"
        "POSITION_SYNC_INTERVAL_SECONDS=30\n"
        "ORDERS_SYNC_INTERVAL_SECONDS=90\n"
        "APP_RUNTIME_STARTUP_VALIDATE=true\n"
        "SCHEMA_CHECK_MODE=strict\n",
        # Note: HUB_DEFAULT_TENANT_ID is intentionally absent
        encoding="utf-8",
    )

    errors = lint_env_profile(profile_path)
    tenant_errors = [e for e in errors if "HUB_DEFAULT_TENANT_ID" in e]
    assert tenant_errors, f"Expected HUB_DEFAULT_TENANT_ID error, got: {errors}"


def test_linter_accepts_live_profile_with_hub_default_tenant_id(tmp_path):
    """LIVE profile with HUB_DEFAULT_TENANT_ID set must not produce a tenant-ID error."""
    profile_path = tmp_path / "with-hub-tenant-live.env"
    profile_path.write_text(
        "APP_ENV=production\n"
        "TRADE_MODE=LIVE\n"
        "BROKER_SECRET_BACKEND=postgres\n"
        "ADMIN_API_KEY=test-admin\n"
        "CONTROL_PLANE_BACKEND=postgres\n"
        "SWEEP_STATE_BACKEND=postgres\n"
        "ENABLE_CAPITAL_CHECKS=true\n"
        "CAPITAL_LIMITS_JSON={\"A1\": {}}\n"
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
        "OWNERSHIP_PERSIST_PENDING_LOCKS=true\n"
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
        "HUB_INSTANCE_NAME=phoenix-live-1\n"
        "HUB_DEFAULT_TENANT_ID=tenant-1\n"
        "LEADER_LEASE_ENABLED=true\n"
        "LEADER_LEASE_BACKEND=postgres\n"
        "CLIENT_LOCAL_IP=10.0.0.1\n"
        "CLIENT_PUBLIC_IP=1.2.3.4\n"
        "MAC_ADDRESS=aa:bb:cc:dd:ee:ff\n"
        "POSITION_SYNC_INTERVAL_SECONDS=30\n"
        "ORDERS_SYNC_INTERVAL_SECONDS=90\n"
        "APP_RUNTIME_STARTUP_VALIDATE=true\n"
        "SCHEMA_CHECK_MODE=strict\n",
        encoding="utf-8",
    )

    errors = lint_env_profile(profile_path)
    tenant_errors = [e for e in errors if "HUB_DEFAULT_TENANT_ID" in e]
    assert tenant_errors == [], f"Unexpected HUB_DEFAULT_TENANT_ID errors: {tenant_errors}"
