"""Sprint 6 OBS-6.2: OpenTelemetry-compatible tracing tests."""

from __future__ import annotations

import pytest

from app.observability.tracing import (
    Span,
    get_current_span_id,
    get_current_trace_id,
    trace_span,
    trace_order_submission,
    trace_broker_call,
    trace_api_request,
)


class TestSpan:
    def test_duration_ms_while_running(self):
        span = Span(trace_id="t1", span_id="s1", parent_span_id=None, operation="test")
        assert span.duration_ms > 0

    def test_duration_ms_after_finish(self):
        span = Span(trace_id="t1", span_id="s1", parent_span_id=None, operation="test")
        span.finish()
        assert span.end_time is not None
        assert span.duration_ms >= 0

    def test_set_attribute(self):
        span = Span(trace_id="t1", span_id="s1", parent_span_id=None, operation="test")
        span.set_attribute("key", "value")
        assert span.attributes["key"] == "value"

    def test_set_status(self):
        span = Span(trace_id="t1", span_id="s1", parent_span_id=None, operation="test")
        span.set_status("ERROR", "something failed")
        assert span.status == "ERROR"
        assert span.attributes["status_message"] == "something failed"

    def test_add_event(self):
        span = Span(trace_id="t1", span_id="s1", parent_span_id=None, operation="test")
        span.add_event("checkpoint", extra="data")
        assert len(span.events) == 1
        assert span.events[0]["name"] == "checkpoint"
        assert span.events[0]["extra"] == "data"


class TestTraceSpan:
    def test_creates_span_with_operation(self):
        with trace_span("test.op") as span:
            assert span.operation == "test.op"
            assert span.trace_id
            assert span.span_id

    def test_span_finished_on_exit(self):
        with trace_span("test.finish") as span:
            pass
        assert span.end_time is not None

    def test_context_propagation(self):
        assert get_current_trace_id() is None
        assert get_current_span_id() is None

        with trace_span("parent") as parent:
            assert get_current_trace_id() == parent.trace_id
            assert get_current_span_id() == parent.span_id

            with trace_span("child") as child:
                assert child.parent_span_id == parent.span_id
                assert child.trace_id == parent.trace_id

        assert get_current_trace_id() is None
        assert get_current_span_id() is None

    def test_explicit_trace_id(self):
        with trace_span("test", trace_id="custom-trace") as span:
            assert span.trace_id == "custom-trace"

    def test_attributes_passed_through(self):
        with trace_span("test", attributes={"k": "v"}) as span:
            assert span.attributes["k"] == "v"

    def test_exception_sets_error_status(self):
        with pytest.raises(ValueError):
            with trace_span("failing") as span:
                raise ValueError("boom")
        assert span.status == "ERROR"
        assert span.attributes["error.type"] == "ValueError"
        assert span.end_time is not None

    def test_context_restored_on_exception(self):
        try:
            with trace_span("failing"):
                raise RuntimeError("oops")
        except RuntimeError:
            pass
        assert get_current_trace_id() is None
        assert get_current_span_id() is None


class TestPreBuiltSpans:
    def test_trace_order_submission(self):
        with trace_order_submission("t1", "ba1", "strat1", "NIFTY") as span:
            assert span.operation == "order.submit"
            assert span.attributes["tenant_id"] == "t1"
            assert span.attributes["symbol"] == "NIFTY"

    def test_trace_broker_call(self):
        with trace_broker_call("place_order", "ba1") as span:
            assert span.operation == "broker.place_order"
            assert span.attributes["broker_account_id"] == "ba1"

    def test_trace_api_request(self):
        with trace_api_request("POST", "/orders") as span:
            assert span.operation == "api.post"
            assert span.attributes["http.method"] == "POST"
            assert span.attributes["http.path"] == "/orders"
