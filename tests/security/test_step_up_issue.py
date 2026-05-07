"""Tests for POST /admin/step-up/issue (#187) and kill_switch rearm step-up (#188)."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(role="operator", caller="test-operator"):
    ctx = SimpleNamespace(caller=caller, role=role)

    def require_role(required):
        hierarchy = {"readonly": 0, "operator": 1, "admin": 2}
        if hierarchy.get(ctx.role, 0) < hierarchy.get(required.value if hasattr(required, "value") else required, 0):
            from fastapi import HTTPException, status
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")

    ctx.require_role = require_role
    return ctx


# ---------------------------------------------------------------------------
# #187 — step_up_issue endpoint unit tests
# ---------------------------------------------------------------------------

class TestStepUpIssueEndpoint:
    def test_issue_valid_action_class(self):
        """Valid action_class returns token with expected fields."""
        from app.dashboard.admin_routes import StepUpIssueRequest, step_up_issue
        from app.security.step_up import purge_expired

        purge_expired()
        req = StepUpIssueRequest(action_class="kill_switch_rearm", resource_id="GLOBAL")
        ctx = _make_ctx(role="operator")

        with patch("app.security.step_up._is_live_mode", return_value=False):
            result = step_up_issue(req, ctx)

        assert "token_id" in result
        assert result["action_class"] == "kill_switch_rearm"
        assert result["resource_id"] == "GLOBAL"
        assert result["actor"] == "test-operator"
        assert result["ttl_seconds"] == pytest.approx(300, abs=2)
        assert result["expires_at"] > time.time()

    def test_issue_unknown_action_class_returns_422(self):
        """Unknown action_class raises 422."""
        from fastapi import HTTPException
        from app.dashboard.admin_routes import StepUpIssueRequest, step_up_issue

        req = StepUpIssueRequest(action_class="not_a_real_action")
        ctx = _make_ctx(role="operator")

        with pytest.raises(HTTPException) as exc_info:
            step_up_issue(req, ctx)
        assert exc_info.value.status_code == 422

    def test_issue_requires_operator_role(self):
        """READONLY role cannot issue step-up tokens."""
        from fastapi import HTTPException
        from app.dashboard.admin_routes import StepUpIssueRequest, step_up_issue

        req = StepUpIssueRequest(action_class="break_glass")
        ctx = _make_ctx(role="readonly")

        with pytest.raises(HTTPException) as exc_info:
            step_up_issue(req, ctx)
        assert exc_info.value.status_code == 403

    def test_issue_all_valid_action_classes(self):
        """Every DangerousActionClass value can be issued."""
        from app.dashboard.admin_routes import StepUpIssueRequest, step_up_issue
        from app.security.step_up import DangerousActionClass, purge_expired

        purge_expired()
        ctx = _make_ctx(role="operator")
        with patch("app.security.step_up._is_live_mode", return_value=False):
            for ac in DangerousActionClass:
                req = StepUpIssueRequest(action_class=ac.value)
                result = step_up_issue(req, ctx)
                assert result["action_class"] == ac.value

    def test_issued_token_is_single_use(self):
        """A token issued via the endpoint can be consumed exactly once."""
        from app.dashboard.admin_routes import StepUpIssueRequest, step_up_issue
        from app.security.step_up import consume_step_up_token, DangerousActionClass, purge_expired

        purge_expired()
        req = StepUpIssueRequest(action_class="break_glass", resource_id="")
        ctx = _make_ctx(role="operator", caller="actor-1")

        with patch("app.security.step_up._is_live_mode", return_value=False):
            result = step_up_issue(req, ctx)

        tid = result["token_id"]
        assert consume_step_up_token(
            token_id=tid, actor="actor-1", action_class=DangerousActionClass.BREAK_GLASS
        )
        # Second consume must fail
        assert not consume_step_up_token(
            token_id=tid, actor="actor-1", action_class=DangerousActionClass.BREAK_GLASS
        )


# ---------------------------------------------------------------------------
# #188 — kill_switch_rearm step-up enforcement
# ---------------------------------------------------------------------------

class TestKillSwitchRearmStepUp:
    def _make_ksm_mock(self):
        record = SimpleNamespace(id="rec-1", state=SimpleNamespace(value="INACTIVE"))
        ksm = MagicMock()
        ksm.rearm.return_value = record
        return ksm

    def test_rearm_paper_mode_no_token_required(self):
        """In PAPER mode, rearm succeeds without step_up_token."""
        from app.dashboard.admin_routes import KillSwitchRearmRequest, kill_switch_rearm

        payload = KillSwitchRearmRequest(scope="GLOBAL", scope_id="GLOBAL")
        ctx = _make_ctx(role="operator")
        ksm = self._make_ksm_mock()

        with patch.dict("os.environ", {"TRADE_MODE": "PAPER"}), \
             patch("app.dashboard.admin_routes._get_kill_switch_manager", return_value=ksm), \
             patch("app.dashboard.admin_routes._save_kill_switch_state"), \
             patch("app.dashboard.admin_routes.emit_audit_event"):
            result = kill_switch_rearm(payload, ctx)

        assert result["status"] == "inactive"

    def test_rearm_live_mode_requires_token(self):
        """In LIVE mode, rearm without step_up_token returns 403."""
        from fastapi import HTTPException
        from app.dashboard.admin_routes import KillSwitchRearmRequest, kill_switch_rearm

        payload = KillSwitchRearmRequest(scope="GLOBAL", scope_id="GLOBAL")
        ctx = _make_ctx(role="operator")

        with patch.dict("os.environ", {"TRADE_MODE": "LIVE"}):
            with pytest.raises(HTTPException) as exc_info:
                kill_switch_rearm(payload, ctx)
        assert exc_info.value.status_code == 403
        assert "step_up_token" in exc_info.value.detail

    def test_rearm_live_mode_invalid_token_returns_403(self):
        """In LIVE mode, a bogus step_up_token returns 403."""
        from fastapi import HTTPException
        from app.dashboard.admin_routes import KillSwitchRearmRequest, kill_switch_rearm

        payload = KillSwitchRearmRequest(
            scope="GLOBAL", scope_id="GLOBAL", step_up_token="not-a-real-token"
        )
        ctx = _make_ctx(role="operator")

        with patch.dict("os.environ", {"TRADE_MODE": "LIVE"}), \
             patch("app.security.step_up._is_live_mode", return_value=False):
            with pytest.raises(HTTPException) as exc_info:
                kill_switch_rearm(payload, ctx)
        assert exc_info.value.status_code == 403

    def test_rearm_live_mode_valid_token_succeeds(self):
        """In LIVE mode, a valid step_up_token allows rearm."""
        from app.dashboard.admin_routes import KillSwitchRearmRequest, kill_switch_rearm
        from app.security.step_up import (
            DangerousActionClass, issue_step_up_token, purge_expired
        )

        purge_expired()
        ctx = _make_ctx(role="operator", caller="ops-user")
        ksm = self._make_ksm_mock()

        with patch("app.security.step_up._is_live_mode", return_value=False):
            tok = issue_step_up_token(
                actor="ops-user",
                action_class=DangerousActionClass.KILL_SWITCH_REARM,
                resource_id="GLOBAL",
            )

        payload = KillSwitchRearmRequest(
            scope="GLOBAL", scope_id="GLOBAL", step_up_token=tok.token_id
        )

        with patch.dict("os.environ", {"TRADE_MODE": "LIVE"}), \
             patch("app.security.step_up._is_live_mode", return_value=False), \
             patch("app.dashboard.admin_routes._get_kill_switch_manager", return_value=ksm), \
             patch("app.dashboard.admin_routes._save_kill_switch_state"), \
             patch("app.dashboard.admin_routes.emit_audit_event"):
            result = kill_switch_rearm(payload, ctx)

        assert result["status"] == "inactive"

    def test_rearm_live_token_single_use(self):
        """A step_up_token for rearm cannot be used twice."""
        from fastapi import HTTPException
        from app.dashboard.admin_routes import KillSwitchRearmRequest, kill_switch_rearm
        from app.security.step_up import (
            DangerousActionClass, issue_step_up_token, purge_expired
        )

        purge_expired()
        ctx = _make_ctx(role="operator", caller="ops-user")
        ksm = self._make_ksm_mock()

        with patch("app.security.step_up._is_live_mode", return_value=False):
            tok = issue_step_up_token(
                actor="ops-user",
                action_class=DangerousActionClass.KILL_SWITCH_REARM,
            )

        payload = KillSwitchRearmRequest(
            scope="GLOBAL", scope_id="GLOBAL", step_up_token=tok.token_id
        )

        # First use succeeds
        with patch.dict("os.environ", {"TRADE_MODE": "LIVE"}), \
             patch("app.security.step_up._is_live_mode", return_value=False), \
             patch("app.dashboard.admin_routes._get_kill_switch_manager", return_value=ksm), \
             patch("app.dashboard.admin_routes._save_kill_switch_state"), \
             patch("app.dashboard.admin_routes.emit_audit_event"):
            kill_switch_rearm(payload, ctx)

        # Second use must fail
        with patch.dict("os.environ", {"TRADE_MODE": "LIVE"}), \
             patch("app.security.step_up._is_live_mode", return_value=False):
            with pytest.raises(HTTPException) as exc_info:
                kill_switch_rearm(payload, ctx)
        assert exc_info.value.status_code == 403
