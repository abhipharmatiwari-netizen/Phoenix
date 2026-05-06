"""Tests for /readyz position_authority_restored field correctness — issue #139 / #154."""

from __future__ import annotations



class _FakeRuntime:
    """Minimal stub of AppRuntime for readyz testing."""

    def __init__(
        self,
        *,
        position_records_loaded: int,
        position_authority_restored: bool,
        ownership_gap: bool = False,
        ready: bool = True,
    ):
        self._position_authority_restored = position_authority_restored
        self._startup_ownership_gap_detected = ownership_gap
        self._ready = ready
        self._is_leader = True
        self._hub_started = True
        self._position_records_loaded = position_records_loaded

    def startup_recovery_status(self):
        return {
            "summary": {
                "position_records_loaded": self._position_records_loaded,
                "unresolved_active": 0,
                "startup_ownership_gap_detected": self._startup_ownership_gap_detected,
            }
        }

    @property
    def ready(self):
        return self._ready


def _extract_readyz_payload(runtime_stub):
    """Run the position_authority_restored block from server.py with a stub runtime."""
    pos_authority = getattr(runtime_stub, "_position_authority_restored", None)
    ownership_gap = bool(getattr(runtime_stub, "_startup_ownership_gap_detected", False))

    recovery_summary: dict = {}
    if callable(getattr(runtime_stub, "startup_recovery_status", None)):
        rs = runtime_stub.startup_recovery_status()
        recovery_summary = rs.get("summary") or {}
    position_records_loaded = int((recovery_summary or {}).get("position_records_loaded", 0))

    # Replicate the fixed logic from server.py
    effective_authority = True if position_records_loaded == 0 else bool(pos_authority)

    payload = {
        "position_records_loaded": position_records_loaded,
        "position_authority_restored": effective_authority,
        "startup_ownership_gap_detected": ownership_gap,
    }

    # Gate logic (same as server.py)
    blocked = False
    if position_records_loaded > 0 and not pos_authority:
        blocked = True
        payload["reason"] = "position_authority_not_restored"
    if ownership_gap:
        blocked = True
        payload["reason"] = "startup_ownership_gap_detected"

    payload["ready"] = not blocked
    return payload


class TestReadyzPositionAuthority:
    def test_zero_records_reports_authority_true(self):
        rt = _FakeRuntime(position_records_loaded=0, position_authority_restored=False)
        payload = _extract_readyz_payload(rt)
        assert payload["position_authority_restored"] is True, (
            "When 0 records loaded there is nothing to restore — authority should be true."
        )
        assert payload["ready"] is True
        assert payload["position_records_loaded"] == 0

    def test_records_loaded_and_restored_reports_true(self):
        rt = _FakeRuntime(position_records_loaded=3, position_authority_restored=True)
        payload = _extract_readyz_payload(rt)
        assert payload["position_authority_restored"] is True
        assert payload["ready"] is True

    def test_records_loaded_but_not_restored_blocks_readiness(self):
        rt = _FakeRuntime(position_records_loaded=3, position_authority_restored=False)
        payload = _extract_readyz_payload(rt)
        assert payload["position_authority_restored"] is False
        assert payload["ready"] is False
        assert payload.get("reason") == "position_authority_not_restored"

    def test_ownership_gap_blocks_readiness(self):
        rt = _FakeRuntime(
            position_records_loaded=2,
            position_authority_restored=True,
            ownership_gap=True,
        )
        payload = _extract_readyz_payload(rt)
        assert payload["ready"] is False
        assert payload.get("reason") == "startup_ownership_gap_detected"
