"""
Sprint 1 security hardening tests.

Validates:
- RBAC role hierarchy and enforcement
- Rate limiting middleware
- Audit log emission
- Auth enforcement for non-local environments
- .env.example template exists and has no real secrets
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    monkeypatch.setenv("CONTROL_PLANE_BACKEND", "postgres")
    monkeypatch.setenv("SWEEP_STATE_BACKEND", "postgres")
    monkeypatch.setenv("BROKER_SECRET_BACKEND", "env")
    monkeypatch.setenv("ENABLE_MULTI_HUB", "false")
    monkeypatch.setenv("DASHBOARD_AUTH_DISABLED", "true")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")


class TestRBACRoleHierarchy:
    """AdminRole hierarchy and permission checks."""

    def test_role_enum_values(self):
        from app.dashboard.auth import AdminRole
        assert AdminRole.READONLY.value == "readonly"
        assert AdminRole.OPERATOR.value == "operator"
        assert AdminRole.ADMIN.value == "admin"

    def test_admin_has_all_permissions(self):
        from app.dashboard.auth import AdminRole, _role_has_permission
        assert _role_has_permission(AdminRole.ADMIN, AdminRole.READONLY)
        assert _role_has_permission(AdminRole.ADMIN, AdminRole.OPERATOR)
        assert _role_has_permission(AdminRole.ADMIN, AdminRole.ADMIN)

    def test_operator_limited_permissions(self):
        from app.dashboard.auth import AdminRole, _role_has_permission
        assert _role_has_permission(AdminRole.OPERATOR, AdminRole.READONLY)
        assert _role_has_permission(AdminRole.OPERATOR, AdminRole.OPERATOR)
        assert not _role_has_permission(AdminRole.OPERATOR, AdminRole.ADMIN)

    def test_readonly_minimal_permissions(self):
        from app.dashboard.auth import AdminRole, _role_has_permission
        assert _role_has_permission(AdminRole.READONLY, AdminRole.READONLY)
        assert not _role_has_permission(AdminRole.READONLY, AdminRole.OPERATOR)
        assert not _role_has_permission(AdminRole.READONLY, AdminRole.ADMIN)

    def test_require_role_raises_403(self):
        from fastapi import HTTPException
        from app.dashboard.auth import AdminContext, AdminRole
        ctx = AdminContext(caller="test", role=AdminRole.READONLY)
        with pytest.raises(HTTPException) as exc_info:
            ctx.require_role(AdminRole.OPERATOR)
        assert exc_info.value.status_code == 403

    def test_require_role_passes_sufficient(self):
        from app.dashboard.auth import AdminContext, AdminRole
        ctx = AdminContext(caller="test", role=AdminRole.ADMIN)
        ctx.require_role(AdminRole.OPERATOR)  # Should not raise

    def test_resolve_role_from_key_suffix(self):
        from app.dashboard.auth import _resolve_role_from_key, AdminRole
        assert _resolve_role_from_key("secret:readonly") == AdminRole.READONLY
        assert _resolve_role_from_key("secret:operator") == AdminRole.OPERATOR
        assert _resolve_role_from_key("secret:admin") == AdminRole.ADMIN
        assert _resolve_role_from_key("secret") == AdminRole.ADMIN
        assert _resolve_role_from_key(None) == AdminRole.ADMIN


class TestRateLimiting:
    """Rate limit middleware behavior."""

    def test_check_rate_limit_import(self):
        from app.core.rate_limit_middleware import check_rate_limit
        assert callable(check_rate_limit)

    def test_check_request_size_import(self):
        from app.core.rate_limit_middleware import check_request_size
        assert callable(check_request_size)


class TestAuditLog:
    """Structured audit log emission."""

    def test_emit_audit_event_returns_record(self):
        from app.core.audit_log import emit_audit_event
        event = emit_audit_event(
            actor="test_user",
            action="test_action",
            resource_type="test",
            resource_id="123",
        )
        assert event["actor"] == "test_user"
        assert event["action"] == "test_action"
        assert event["resource_type"] == "test"
        assert event["resource_id"] == "123"
        assert "audit_id" in event
        assert "timestamp" in event

    def test_emit_with_before_after(self):
        from app.core.audit_log import emit_audit_event
        event = emit_audit_event(
            actor="admin",
            action="update",
            resource_type="config",
            resource_id="x",
            before={"enabled": False},
            after={"enabled": True},
        )
        assert event["before"] == {"enabled": False}
        assert event["after"] == {"enabled": True}

    def test_emit_serializes_datetime(self):
        from app.core.audit_log import emit_audit_event
        now = datetime.now(timezone.utc)
        event = emit_audit_event(
            actor="admin",
            action="create",
            resource_type="tenant",
            resource_id="t1",
            after={"created_at": now},
        )
        assert isinstance(event["after"]["created_at"], str)

    def test_audit_log_is_queryable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
        from app.core.audit_log import emit_audit_event, list_audit_events
        event = emit_audit_event(
            actor="admin",
            action="toggle_strategy",
            resource_type="strategy",
            resource_id="ce_orb",
            after={"enabled": True},
        )
        events = list_audit_events(limit=10, action="toggle_strategy")
        assert events
        assert events[0]["audit_id"] == event["audit_id"]
        assert events[0]["after"]["enabled"] is True

    def test_emit_uses_request_context_when_request_id_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
        from app.core.audit_log import emit_audit_event
        from app.core.logging_utils import reset_log_context, set_log_context

        tokens = set_log_context(correlation_id="corr-123", request_id="req-123")
        try:
            event = emit_audit_event(
                actor="admin",
                action="update",
                resource_type="tenant",
                resource_id="tenant-1",
            )
        finally:
            reset_log_context(tokens)

        assert event["request_id"] == "req-123"
        assert event["metadata"]["correlation_id"] == "corr-123"

    def test_emit_promotes_idempotency_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
        from app.core.audit_log import emit_audit_event

        event = emit_audit_event(
            actor="admin",
            action="manual_exit",
            resource_type="order",
            resource_id="oid-1",
            metadata={"idempotency_key": "idem-123"},
        )

        assert event["idempotency_key"] == "idem-123"


class TestAuthEnforcement:
    """Non-local auth enforcement logic."""

    def test_auth_required_in_production(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("DASHBOARD_AUTH_DISABLED", "true")
        from app.dashboard.auth import _is_auth_required
        assert _is_auth_required() is True

    def test_auth_required_in_staging(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "staging")
        monkeypatch.setenv("DASHBOARD_AUTH_DISABLED", "true")
        from app.dashboard.auth import _is_auth_required
        assert _is_auth_required() is True

    def test_auth_optional_in_local_dev(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "local")
        monkeypatch.setenv("DASHBOARD_AUTH_DISABLED", "true")
        from app.dashboard.auth import _is_auth_required
        assert _is_auth_required() is False

    def test_auth_required_in_local_when_not_disabled(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "local")
        monkeypatch.setenv("DASHBOARD_AUTH_DISABLED", "false")
        from app.dashboard.auth import _is_auth_required
        assert _is_auth_required() is True


class TestEnvTemplates:
    """Repo-shipped env templates remain placeholder-only."""

    def test_committed_env_templates_exist(self):
        repo_root = Path(__file__).resolve().parents[2]
        assert (repo_root / "docker.env").is_file()
        assert (repo_root / "docs" / "runbooks" / "docker-live.env.example").is_file()

    def test_committed_env_templates_have_no_real_secrets(self):
        repo_root = Path(__file__).resolve().parents[2]
        contents = [
            (repo_root / "docker.env").read_text(encoding="utf-8"),
            (
                repo_root / "docs" / "runbooks" / "docker-live.env.example"
            ).read_text(encoding="utf-8"),
        ]
        for content in contents:
            assert "022@Mumbai" not in content
        assert any("CHANGE_ME" in content for content in contents)

    def test_committed_env_templates_have_key_sections(self):
        repo_root = Path(__file__).resolve().parents[2]
        live_template = (
            repo_root / "docs" / "runbooks" / "docker-live.env.example"
        ).read_text(encoding="utf-8")
        assert "ADMIN_API_KEY" in live_template
        assert "CONTROL_PLANE_PG_PASSWORD" in live_template
        assert "TRADE_MODE=LIVE" in live_template


class TestSupplyChain:
    """Dependency scanning and pinning artifacts exist."""

    def test_security_scan_workflow_exists(self):
        path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            ".github",
            "workflows",
            "security-scan.yml",
        )
        assert os.path.isfile(path), "security scan workflow should exist"
        content = open(path).read().lower()
        assert "gitleaks" in content
        assert "pip-audit" in content or "pip_audit" in content
        assert "generate_sbom.py" in content

    def test_requirements_are_pinned(self):
        path = os.path.join(os.path.dirname(__file__), "..", "..", "requirements.txt")
        lines = [
            line.strip()
            for line in open(path).read().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert lines
        unpinned = [line for line in lines if "==" not in line]
        assert not unpinned, f"requirements.txt must pin versions exactly: {unpinned}"


class TestSafeCompareToken:
    """Timing-safe token comparison."""

    def test_matching_tokens(self):
        from app.dashboard.auth import safe_compare_token
        assert safe_compare_token("secret123", "secret123") is True

    def test_mismatched_tokens(self):
        from app.dashboard.auth import safe_compare_token
        assert safe_compare_token("secret123", "wrong") is False

    def test_empty_tokens(self):
        from app.dashboard.auth import safe_compare_token
        assert safe_compare_token("", "") is False
        assert safe_compare_token(None, None) is False
        assert safe_compare_token("valid", "") is False
