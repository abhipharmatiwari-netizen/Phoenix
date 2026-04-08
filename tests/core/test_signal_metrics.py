import logging

from app.core.signal_metrics import (
    SignalSummaryMetrics,
    is_submitted_order_response,
)


def test_is_submitted_order_response_handles_statuses():
    assert is_submitted_order_response(status="REJECTED", broker_order_id="") is False
    assert is_submitted_order_response(status="FAILED", broker_order_id=None) is False
    assert is_submitted_order_response(status="NOT_PLACED", broker_order_id="") is False
    assert is_submitted_order_response(status="PLACED", broker_order_id="") is True
    assert is_submitted_order_response(status="REJECTED", broker_order_id="OID-1") is True


def test_signal_summary_metrics_tracks_window_and_totals(caplog):
    metrics = SignalSummaryMetrics()
    metrics.mark_signal_checked(3)
    metrics.mark_signal_fired(2)
    metrics.mark_order_submitted(1)

    with caplog.at_level(logging.INFO):
        snapshot = metrics.maybe_log_summary(
            logging.getLogger("tests.signal_metrics"),
            interval_seconds=60,
            force=True,
        )

    assert snapshot is not None
    assert snapshot.window_signals_checked == 3
    assert snapshot.window_signals_fired == 2
    assert snapshot.window_orders_submitted == 1
    assert snapshot.total_signals_checked == 3
    assert snapshot.total_signals_fired == 2
    assert snapshot.total_orders_submitted == 1
    assert "SIGNAL_SUMMARY" in caplog.text

    post = metrics.snapshot()
    assert post.window_signals_checked == 0
    assert post.window_signals_fired == 0
    assert post.window_orders_submitted == 0
    assert post.total_signals_checked == 3
    assert post.total_signals_fired == 2
    assert post.total_orders_submitted == 1
