"""Tests for reconciliation timeout watcher (M2)."""

import time

from app.core.reconciliation_timeout_watcher import (
    ReconciliationTimeoutWatcher,
)


class TestReconciliationTimeoutWatcher:
    def test_mark_reconciling_registers_scope(self):
        watcher = ReconciliationTimeoutWatcher(timeout_seconds=10.0)
        watcher.mark_reconciling("scope1")
        assert len(watcher.active_scopes()) == 1

    def test_mark_resolved_removes_scope(self):
        watcher = ReconciliationTimeoutWatcher(timeout_seconds=10.0)
        watcher.mark_reconciling("scope1")
        watcher.mark_resolved("scope1")
        assert len(watcher.active_scopes()) == 0

    def test_is_stale_returns_false_when_fresh(self):
        watcher = ReconciliationTimeoutWatcher(timeout_seconds=100.0)
        watcher.mark_reconciling("scope1")
        assert watcher.is_stale("scope1") is False

    def test_is_stale_returns_true_when_exceeded(self):
        mono = [0.0]
        watcher = ReconciliationTimeoutWatcher(timeout_seconds=5.0)
        watcher._now_mono = lambda: mono[0]
        watcher.mark_reconciling("scope1")
        mono[0] = 10.0
        assert watcher.is_stale("scope1") is True

    def test_stale_count(self):
        mono = [0.0]
        watcher = ReconciliationTimeoutWatcher(timeout_seconds=5.0)
        watcher._now_mono = lambda: mono[0]
        watcher.mark_reconciling("scope1")
        watcher.mark_reconciling("scope2")
        assert watcher.stale_count() == 0
        mono[0] = 10.0
        assert watcher.stale_count() == 2

    def test_escalation_callback_fires(self):
        mono = [0.0]
        escalated = []
        watcher = ReconciliationTimeoutWatcher(
            timeout_seconds=5.0,
            on_escalate=lambda key, age, meta: escalated.append(key),
        )
        watcher._now_mono = lambda: mono[0]
        watcher.mark_reconciling("scope1")
        mono[0] = 10.0
        watcher._check_timeouts()
        assert "scope1" in escalated

    def test_escalation_only_fires_once(self):
        mono = [0.0]
        escalated = []
        watcher = ReconciliationTimeoutWatcher(
            timeout_seconds=5.0,
            on_escalate=lambda key, age, meta: escalated.append(key),
        )
        watcher._now_mono = lambda: mono[0]
        watcher.mark_reconciling("scope1")
        mono[0] = 10.0
        watcher._check_timeouts()
        watcher._check_timeouts()
        assert len(escalated) == 1

    def test_entry_freeze_callback_fires(self):
        mono = [0.0]
        frozen = []
        watcher = ReconciliationTimeoutWatcher(
            timeout_seconds=5.0,
            on_entry_freeze=lambda key: frozen.append(key),
        )
        watcher._now_mono = lambda: mono[0]
        watcher.mark_reconciling("scope1")
        mono[0] = 10.0
        watcher._check_timeouts()
        assert "scope1" in frozen

    def test_unknown_scope_not_stale(self):
        watcher = ReconciliationTimeoutWatcher(timeout_seconds=5.0)
        assert watcher.is_stale("nonexistent") is False
