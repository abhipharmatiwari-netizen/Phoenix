from __future__ import annotations

import logging

from app.core.logging_utils import log_event, reset_log_context, set_log_context


def test_log_event_emits_structured_fields(caplog):
    event_logger = logging.getLogger("tests.logging_utils.structured")
    with caplog.at_level(logging.INFO, logger=event_logger.name):
        log_event(
            event_logger,
            event_type="ORDER_PLACED",
            message="order routed",
            tenant_id="tenant-1",
            broker_account_id="acct-1",
            strategy_id="strategy-1",
            correlation_id="corr-1",
            request_id="req-1",
            instrument="SBIN",
            source="unit-test",
        )
    assert caplog.records
    text = caplog.records[-1].getMessage()
    assert "event_type=ORDER_PLACED" in text
    assert "tenant_id=tenant-1" in text
    assert "broker_account_id=acct-1" in text
    assert "strategy_id=strategy-1" in text
    assert "correlation_id=corr-1" in text
    assert "request_id=req-1" in text
    assert "instrument=SBIN" in text
    assert "source=unit-test" in text


def test_log_event_uses_context_correlation_fields(caplog):
    event_logger = logging.getLogger("tests.logging_utils.context")
    tokens = set_log_context(correlation_id="ctx-corr", request_id="ctx-req")
    try:
        with caplog.at_level(logging.INFO, logger=event_logger.name):
            log_event(
                event_logger,
                event_type="PING",
                message="context fields should be present",
            )
    finally:
        reset_log_context(tokens)
    text = caplog.records[-1].getMessage()
    assert "correlation_id=ctx-corr" in text
    assert "request_id=ctx-req" in text
