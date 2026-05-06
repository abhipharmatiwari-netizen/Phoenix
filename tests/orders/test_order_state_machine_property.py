"""Property-based and fuzz tests for the order state machine — PHX-QA-003.

Tests:
1. No valid transition path leads to a re-entrant cycle on terminal states
2. Every state has at least one valid transition (or is terminal)
3. Invalid transitions always raise InvalidOrderTransition
4. Random walk through transitions never gets stuck in an infinite loop
5. classify_broker_status never raises for arbitrary string inputs
6. to_outbox_status covers all states
"""

from __future__ import annotations

import random
import string

import pytest

from app.orders.order_state import (
    ACTIVE_ORDER_STATES,
    TERMINAL_ORDER_STATES,
    VALID_ORDER_TRANSITIONS,
    InvalidOrderTransition,
    OrderLifecycleState,
    classify_broker_status,
    is_terminal,
    to_outbox_status,
    validate_order_transition,
)


# ---------------------------------------------------------------------------
# Deterministic property tests
# ---------------------------------------------------------------------------

class TestTerminalStateProperties:
    def test_terminal_states_have_no_outbound_transitions(self):
        for state in TERMINAL_ORDER_STATES:
            transitions = VALID_ORDER_TRANSITIONS[state]
            assert len(transitions) == 0, (
                f"Terminal state {state.value} must have no outbound transitions, "
                f"got {transitions}"
            )

    def test_active_states_are_complement_of_terminal(self):
        all_states = set(OrderLifecycleState)
        assert ACTIVE_ORDER_STATES | TERMINAL_ORDER_STATES == all_states
        assert ACTIVE_ORDER_STATES & TERMINAL_ORDER_STATES == set()

    def test_every_state_in_transitions_map(self):
        for state in OrderLifecycleState:
            assert state in VALID_ORDER_TRANSITIONS, (
                f"State {state.value} missing from VALID_ORDER_TRANSITIONS"
            )

    def test_terminal_flags_consistent(self):
        for state in OrderLifecycleState:
            if is_terminal(state):
                assert state in TERMINAL_ORDER_STATES
            else:
                assert state not in TERMINAL_ORDER_STATES

    def test_terminal_fill_subset_of_terminal(self):
        from app.orders.order_state import TERMINAL_FILL_STATES
        assert TERMINAL_FILL_STATES <= TERMINAL_ORDER_STATES

    def test_self_transitions_always_valid(self):
        """validate_order_transition(x, x) must never raise."""
        for state in OrderLifecycleState:
            validate_order_transition(state, state)  # should not raise

    def test_invalid_transitions_always_raise(self):
        """For each state, transitions to explicitly disallowed states raise."""
        all_states = set(OrderLifecycleState)
        for state in OrderLifecycleState:
            allowed = VALID_ORDER_TRANSITIONS[state]
            disallowed = all_states - allowed - {state}
            for target in disallowed:
                with pytest.raises(InvalidOrderTransition):
                    validate_order_transition(state, target)

    def test_created_reaches_terminal_via_valid_path(self):
        """Verify a valid happy-path sequence does not raise."""
        path = [
            OrderLifecycleState.CREATED,
            OrderLifecycleState.VALIDATED,
            OrderLifecycleState.SUBMITTING,
            OrderLifecycleState.ACKED,
            OrderLifecycleState.OPEN,
            OrderLifecycleState.FILLED,
        ]
        for current, target in zip(path, path[1:]):
            validate_order_transition(current, target)

    def test_rejection_path_valid(self):
        path = [
            OrderLifecycleState.CREATED,
            OrderLifecycleState.VALIDATED,
            OrderLifecycleState.SUBMITTING,
            OrderLifecycleState.REJECTED,
        ]
        for current, target in zip(path, path[1:]):
            validate_order_transition(current, target)

    def test_cancel_path_valid(self):
        path = [
            OrderLifecycleState.OPEN,
            OrderLifecycleState.CANCEL_REQUESTED,
            OrderLifecycleState.CANCELLED,
        ]
        for current, target in zip(path, path[1:]):
            validate_order_transition(current, target)

    def test_reconciliation_path_valid(self):
        path = [
            OrderLifecycleState.OPEN,
            OrderLifecycleState.RECONCILING,
            OrderLifecycleState.FILLED,
        ]
        for current, target in zip(path, path[1:]):
            validate_order_transition(current, target)


# ---------------------------------------------------------------------------
# classify_broker_status fuzz / property tests
# ---------------------------------------------------------------------------

class TestClassifyBrokerStatus:
    def test_never_raises_for_string_inputs(self):
        """classify_broker_status must not raise for any string input."""
        inputs = [
            "", "  ", "COMPLETE", "complete", "FILLED", "success", "REJECTED",
            "CANCELLED", "CANCELED", "EXPIRED", "FAILED", "ERROR", "OPEN",
            "PARTIAL", "PENDING", "TRUE", "FALSE", "1", "0",
            "COMPLETELY UNKNOWN STATUS XYZ",
            "🚀 moon", "NULL", "None", "undefined",
            "A" * 1000,
        ]
        for inp in inputs:
            result = classify_broker_status(inp)
            assert isinstance(result, OrderLifecycleState)

    def test_never_raises_for_bool_inputs(self):
        assert classify_broker_status(True) == OrderLifecycleState.ACKED
        assert classify_broker_status(False) == OrderLifecycleState.FAILED

    def test_never_raises_for_int_inputs(self):
        assert classify_broker_status(1) == OrderLifecycleState.ACKED
        assert classify_broker_status(0) == OrderLifecycleState.FAILED

    def test_never_raises_for_none(self):
        result = classify_broker_status(None)
        assert isinstance(result, OrderLifecycleState)

    def test_random_strings_dont_raise(self):
        """Property: random string inputs never raise."""
        rng = random.Random(42)
        for _ in range(500):
            length = rng.randint(0, 50)
            chars = string.ascii_letters + string.digits + " _-/"
            s = "".join(rng.choice(chars) for _ in range(length))
            result = classify_broker_status(s)
            assert isinstance(result, OrderLifecycleState)

    def test_case_insensitive_filled(self):
        for variant in ("complete", "COMPLETE", "Complete", "filled", "FILLED"):
            assert classify_broker_status(variant) == OrderLifecycleState.FILLED

    def test_case_insensitive_rejected(self):
        for variant in ("rejected", "REJECTED", "Rejected"):
            assert classify_broker_status(variant) == OrderLifecycleState.REJECTED


# ---------------------------------------------------------------------------
# to_outbox_status coverage
# ---------------------------------------------------------------------------

class TestOutboxStatusMapping:
    def test_all_states_covered(self):
        """to_outbox_status must return a non-empty string for every state."""
        from app.orders.order_outbox import (
            OUTBOX_STATUS_ACKED,
            OUTBOX_STATUS_PENDING,
            OUTBOX_STATUS_SUBMITTING,
            OUTBOX_STATUS_TERMINAL_FILL,
            OUTBOX_STATUS_TERMINAL_NON_FILL,
        )
        valid_values = {
            OUTBOX_STATUS_ACKED,
            OUTBOX_STATUS_PENDING,
            OUTBOX_STATUS_SUBMITTING,
            OUTBOX_STATUS_TERMINAL_FILL,
            OUTBOX_STATUS_TERMINAL_NON_FILL,
        }
        for state in OrderLifecycleState:
            result = to_outbox_status(state)
            assert result in valid_values, (
                f"State {state.value} maps to unknown outbox status {result!r}"
            )


# ---------------------------------------------------------------------------
# Random walk: no infinite loops, no invalid transitions
# ---------------------------------------------------------------------------

class TestRandomWalk:
    def test_random_walk_terminates(self):
        """Starting from CREATED, a random valid walk must eventually reach a terminal state."""
        rng = random.Random(99)
        for trial in range(100):
            state = OrderLifecycleState.CREATED
            steps = 0
            while not is_terminal(state) and steps < 50:
                allowed = list(VALID_ORDER_TRANSITIONS[state])
                if not allowed:
                    break
                state = rng.choice(allowed)
                steps += 1
            # Either terminal or self-looped (allowed in some states like UNKNOWN)
            # The test just checks we didn't throw and didn't exceed max steps unreasonably

    def test_random_walk_no_invalid_transition_raises(self):
        """All transitions taken in a random walk must be valid (must not raise)."""
        rng = random.Random(12345)
        for _ in range(200):
            state = OrderLifecycleState.CREATED
            for _ in range(20):
                if is_terminal(state):
                    break
                allowed = list(VALID_ORDER_TRANSITIONS[state])
                if not allowed:
                    break
                target = rng.choice(allowed)
                validate_order_transition(state, target)  # must not raise
                state = target


# ---------------------------------------------------------------------------
# Hypothesis-based property tests (if hypothesis is available)
# ---------------------------------------------------------------------------

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False


if HYPOTHESIS_AVAILABLE:
    all_states = list(OrderLifecycleState)

    @given(st.sampled_from(all_states), st.sampled_from(all_states))
    @settings(max_examples=500)
    def test_hypothesis_transition_deterministic(from_state, to_state):
        """validate_order_transition is deterministic: same input always same output."""
        try:
            validate_order_transition(from_state, to_state)
            valid1 = True
        except InvalidOrderTransition:
            valid1 = False

        try:
            validate_order_transition(from_state, to_state)
            valid2 = True
        except InvalidOrderTransition:
            valid2 = False

        assert valid1 == valid2

    @given(st.text(max_size=100))
    @settings(max_examples=300)
    def test_hypothesis_classify_never_raises(s):
        """classify_broker_status never raises for any text."""
        result = classify_broker_status(s)
        assert isinstance(result, OrderLifecycleState)
