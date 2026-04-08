"""Chaos test infrastructure (Architecture S16.2, C4).

Provides simulation harnesses for:
1. Broker outage (mock returns errors/timeouts)
2. Database outage (Postgres unreachable mid-operation)
3. Lease loss (force lease expiry during pending submission)
4. Network partition (broker reachable but responses delayed)
"""

import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Optional
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# 1. Broker outage simulator
# ---------------------------------------------------------------------------

class BrokerOutageSimulator:
    """Simulates broker API returning errors or timeouts."""

    def __init__(self) -> None:
        self._active = False
        self._error_type = "timeout"  # "timeout", "error", "partial"
        self._lock = threading.Lock()

    def activate(self, error_type: str = "timeout") -> None:
        with self._lock:
            self._active = True
            self._error_type = error_type

    def deactivate(self) -> None:
        with self._lock:
            self._active = False

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def maybe_fail(self, operation: str = "submit_order") -> None:
        with self._lock:
            if not self._active:
                return
        if self._error_type == "timeout":
            raise TimeoutError(f"Chaos: broker timeout during {operation}")
        elif self._error_type == "error":
            raise ConnectionError(f"Chaos: broker connection error during {operation}")
        elif self._error_type == "partial":
            raise IOError(f"Chaos: broker partial response during {operation}")

    @contextmanager
    def outage(self, error_type: str = "timeout"):
        self.activate(error_type)
        try:
            yield self
        finally:
            self.deactivate()


# ---------------------------------------------------------------------------
# 2. Database outage simulator
# ---------------------------------------------------------------------------

class DatabaseOutageSimulator:
    """Simulates Postgres becoming unreachable mid-operation."""

    def __init__(self) -> None:
        self._active = False
        self._lock = threading.Lock()
        self._fail_after_n_queries = 0
        self._query_count = 0

    def activate(self, *, fail_after_n_queries: int = 0) -> None:
        with self._lock:
            self._active = True
            self._fail_after_n_queries = fail_after_n_queries
            self._query_count = 0

    def deactivate(self) -> None:
        with self._lock:
            self._active = False
            self._query_count = 0

    def maybe_fail(self, operation: str = "query") -> None:
        with self._lock:
            if not self._active:
                return
            self._query_count += 1
            if self._fail_after_n_queries > 0 and self._query_count <= self._fail_after_n_queries:
                return
        raise ConnectionError(f"Chaos: database unreachable during {operation}")

    @contextmanager
    def outage(self, *, fail_after_n_queries: int = 0):
        self.activate(fail_after_n_queries=fail_after_n_queries)
        try:
            yield self
        finally:
            self.deactivate()


# ---------------------------------------------------------------------------
# 3. Lease loss simulator
# ---------------------------------------------------------------------------

class LeaseLossSimulator:
    """Forces leader lease expiry during pending submission."""

    def __init__(self, lease: Optional[Any] = None) -> None:
        self._lease = lease
        self._active = False
        self._lock = threading.Lock()

    def activate(self) -> None:
        with self._lock:
            self._active = True

    def deactivate(self) -> None:
        with self._lock:
            self._active = False

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def check_lease(self) -> bool:
        """Returns False if lease is simulated as lost."""
        with self._lock:
            if self._active:
                return False
        return True

    @contextmanager
    def lease_loss(self):
        self.activate()
        try:
            yield self
        finally:
            self.deactivate()


# ---------------------------------------------------------------------------
# 4. Network partition simulator
# ---------------------------------------------------------------------------

class NetworkPartitionSimulator:
    """Simulates network partition: broker reachable but responses delayed."""

    def __init__(self, delay_seconds: float = 30.0) -> None:
        self._active = False
        self._delay = delay_seconds
        self._lock = threading.Lock()

    def activate(self, delay_seconds: Optional[float] = None) -> None:
        with self._lock:
            self._active = True
            if delay_seconds is not None:
                self._delay = delay_seconds

    def deactivate(self) -> None:
        with self._lock:
            self._active = False

    def maybe_delay(self, operation: str = "request") -> None:
        with self._lock:
            if not self._active:
                return
            delay = self._delay
        time.sleep(delay)

    @contextmanager
    def partition(self, delay_seconds: Optional[float] = None):
        self.activate(delay_seconds)
        try:
            yield self
        finally:
            self.deactivate()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBrokerOutageSimulator:
    def test_inactive_does_not_fail(self):
        sim = BrokerOutageSimulator()
        sim.maybe_fail()  # Should not raise

    def test_timeout_raises(self):
        sim = BrokerOutageSimulator()
        sim.activate("timeout")
        try:
            sim.maybe_fail()
            assert False, "Expected TimeoutError"
        except TimeoutError:
            pass
        sim.deactivate()

    def test_error_raises(self):
        sim = BrokerOutageSimulator()
        with sim.outage("error"):
            try:
                sim.maybe_fail()
                assert False, "Expected ConnectionError"
            except ConnectionError:
                pass

    def test_context_manager_deactivates(self):
        sim = BrokerOutageSimulator()
        with sim.outage():
            assert sim.is_active
        assert not sim.is_active


class TestDatabaseOutageSimulator:
    def test_inactive_does_not_fail(self):
        sim = DatabaseOutageSimulator()
        sim.maybe_fail()  # Should not raise

    def test_immediate_failure(self):
        sim = DatabaseOutageSimulator()
        sim.activate()
        try:
            sim.maybe_fail()
            assert False, "Expected ConnectionError"
        except ConnectionError:
            pass
        sim.deactivate()

    def test_fail_after_n_queries(self):
        sim = DatabaseOutageSimulator()
        with sim.outage(fail_after_n_queries=2):
            sim.maybe_fail()  # query 1 - ok
            sim.maybe_fail()  # query 2 - ok
            try:
                sim.maybe_fail()  # query 3 - fail
                assert False, "Expected ConnectionError"
            except ConnectionError:
                pass


class TestLeaseLossSimulator:
    def test_lease_healthy_by_default(self):
        sim = LeaseLossSimulator()
        assert sim.check_lease() is True

    def test_lease_lost_when_active(self):
        sim = LeaseLossSimulator()
        with sim.lease_loss():
            assert sim.check_lease() is False
        assert sim.check_lease() is True


class TestNetworkPartitionSimulator:
    def test_no_delay_when_inactive(self):
        sim = NetworkPartitionSimulator(delay_seconds=0.01)
        start = time.monotonic()
        sim.maybe_delay()
        elapsed = time.monotonic() - start
        assert elapsed < 0.05

    def test_delay_when_active(self):
        sim = NetworkPartitionSimulator(delay_seconds=0.05)
        with sim.partition(delay_seconds=0.05):
            start = time.monotonic()
            sim.maybe_delay()
            elapsed = time.monotonic() - start
            assert elapsed >= 0.04
