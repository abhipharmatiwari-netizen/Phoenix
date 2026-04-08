"""Tests for OperatingMode enum and authority-path validation (C1/M1)."""

import pytest

from types import SimpleNamespace

from app.core.operating_mode import (
    OperatingMode,
    assert_authority_path_match,
    resolve_operating_mode,
    resolve_operating_mode_from_runtime,
    validate_operating_mode_at_startup,
)


class TestResolveOperatingMode:
    def test_hub_authoritative_with_stream_worker_enabled(self):
        mode = resolve_operating_mode(
            enable_multi_hub=True,
            use_hub_router=True,
            disable_stream_worker=False,
        )
        assert mode == OperatingMode.HUB_AUTHORITATIVE

    def test_hub_authoritative_with_stream_worker_disabled(self):
        mode = resolve_operating_mode(
            enable_multi_hub=True,
            use_hub_router=True,
            disable_stream_worker=True,
        )
        assert mode == OperatingMode.HUB_AUTHORITATIVE

    def test_legacy_authoritative(self):
        mode = resolve_operating_mode(
            enable_multi_hub=False,
            use_hub_router=False,
            disable_stream_worker=False,
        )
        assert mode == OperatingMode.LEGACY_AUTHORITATIVE

    def test_legacy_authoritative_with_stream_worker_disabled(self):
        mode = resolve_operating_mode(
            enable_multi_hub=False,
            use_hub_router=False,
            disable_stream_worker=True,
        )
        assert mode == OperatingMode.LEGACY_AUTHORITATIVE

    def test_ambiguous_mixed_hub_router(self):
        with pytest.raises(ValueError, match="Ambiguous"):
            resolve_operating_mode(
                enable_multi_hub=True,
                use_hub_router=False,
            )

    def test_ambiguous_mixed_hub_router_other(self):
        with pytest.raises(ValueError, match="Ambiguous"):
            resolve_operating_mode(
                enable_multi_hub=False,
                use_hub_router=True,
            )

    def test_ambiguous_partial_hub_with_disable_stream(self):
        """Only one hub flag + disable_stream_worker -> ambiguous."""
        with pytest.raises(ValueError, match="Ambiguous"):
            resolve_operating_mode(
                enable_multi_hub=True,
                use_hub_router=False,
                disable_stream_worker=True,
            )


class TestAssertAuthorityPathMatch:
    def test_hub_path_in_hub_mode_ok(self):
        assert_authority_path_match(
            current_mode=OperatingMode.HUB_AUTHORITATIVE,
            caller_path="hub",
            scope_key="test",
            action="test",
        )

    def test_legacy_path_in_hub_mode_blocked(self):
        with pytest.raises(RuntimeError, match="Cross-authority"):
            assert_authority_path_match(
                current_mode=OperatingMode.HUB_AUTHORITATIVE,
                caller_path="legacy",
                scope_key="test",
                action="test",
            )

    def test_legacy_path_in_legacy_mode_ok(self):
        assert_authority_path_match(
            current_mode=OperatingMode.LEGACY_AUTHORITATIVE,
            caller_path="legacy",
            scope_key="test",
            action="test",
        )

    def test_hub_path_in_legacy_mode_blocked(self):
        with pytest.raises(RuntimeError, match="Cross-authority"):
            assert_authority_path_match(
                current_mode=OperatingMode.LEGACY_AUTHORITATIVE,
                caller_path="hub",
                scope_key="test",
                action="test",
            )


class TestOperatingModeAuthorityPath:
    def test_hub_authority_path(self):
        assert OperatingMode.HUB_AUTHORITATIVE.authority_path == "hub"

    def test_legacy_authority_path(self):
        assert OperatingMode.LEGACY_AUTHORITATIVE.authority_path == "legacy"


class TestResolveOperatingModeFromRuntime:
    def test_hub_mode(self):
        settings = SimpleNamespace(enable_multi_hub=True, use_hub_router=True)
        runtime_cfg = SimpleNamespace(disable_stream_worker=False)
        result = resolve_operating_mode_from_runtime(
            settings=settings, runtime_cfg=runtime_cfg,
        )
        assert result.mode == OperatingMode.HUB_AUTHORITATIVE
        assert result.authority_path == "hub"
        assert "disable_stream_worker=False" in result.reason

    def test_legacy_mode(self):
        settings = SimpleNamespace(enable_multi_hub=False, use_hub_router=False)
        runtime_cfg = SimpleNamespace(disable_stream_worker=True)
        result = resolve_operating_mode_from_runtime(
            settings=settings, runtime_cfg=runtime_cfg,
        )
        assert result.mode == OperatingMode.LEGACY_AUTHORITATIVE
        assert result.authority_path == "legacy"

    def test_ambiguous_returns_none_mode(self):
        settings = SimpleNamespace(enable_multi_hub=True, use_hub_router=False)
        runtime_cfg = SimpleNamespace(disable_stream_worker=False)
        result = resolve_operating_mode_from_runtime(
            settings=settings, runtime_cfg=runtime_cfg,
        )
        assert result.mode is None
        assert result.authority_path == "unknown"
        assert "Ambiguous" in result.reason

    def test_hub_with_stream_worker_enabled_still_resolves_hub(self):
        settings = SimpleNamespace(enable_multi_hub=True, use_hub_router=True)
        runtime_cfg = SimpleNamespace(disable_stream_worker=False)
        result = resolve_operating_mode_from_runtime(
            settings=settings, runtime_cfg=runtime_cfg,
        )
        assert result.mode == OperatingMode.HUB_AUTHORITATIVE
        assert result.authority_path == "hub"

    def test_to_log_dict(self):
        settings = SimpleNamespace(enable_multi_hub=True, use_hub_router=True)
        runtime_cfg = SimpleNamespace(disable_stream_worker=False)
        result = resolve_operating_mode_from_runtime(
            settings=settings, runtime_cfg=runtime_cfg,
        )
        log = result.to_log_dict()
        assert log["mode"] == "HUB_AUTHORITATIVE"
        assert log["authority_path"] == "hub"
        assert "reason" in log


class TestValidateOperatingModeAtStartup:
    def test_prefers_runtime_cfg_disable_stream_worker(self):
        settings = SimpleNamespace(
            enable_multi_hub=True,
            use_hub_router=True,
            disable_stream_worker=True,
        )
        runtime_cfg = SimpleNamespace(disable_stream_worker=False)

        result = validate_operating_mode_at_startup(
            settings=settings,
            trade_mode="LIVE",
            runtime_cfg=runtime_cfg,
        )

        assert result == OperatingMode.HUB_AUTHORITATIVE

    def test_live_raises_only_when_hub_router_flags_are_mixed(self):
        settings = SimpleNamespace(
            enable_multi_hub=True,
            use_hub_router=False,
            disable_stream_worker=False,
        )
        runtime_cfg = SimpleNamespace(disable_stream_worker=False)

        with pytest.raises(ValueError, match="Ambiguous operating mode"):
            validate_operating_mode_at_startup(
                settings=settings,
                trade_mode="LIVE",
                runtime_cfg=runtime_cfg,
            )
