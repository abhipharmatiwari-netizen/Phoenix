from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.brokers.base import (
    OrderPurpose,
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.orders import order_outbox as outbox_mod


def test_build_order_submission_outbox_falls_back_to_memory_when_postgres_init_fails_non_strict(
    monkeypatch,
):
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    monkeypatch.setenv("ORDER_SUBMISSION_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("ORDER_SUBMISSION_OUTBOX_BACKEND", "postgres")
    monkeypatch.setenv("ORDER_SUBMISSION_OUTBOX_REQUIRED", "false")
    monkeypatch.setattr(
        outbox_mod,
        "get_settings",
        lambda: SimpleNamespace(control_plane_backend="postgres"),
    )
    monkeypatch.setattr(
        outbox_mod,
        "get_control_plane_dsn",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("broken dsn")),
    )

    store = outbox_mod.build_order_submission_outbox()

    assert isinstance(store, outbox_mod.InMemoryOrderSubmissionOutbox)


def test_build_order_submission_outbox_raises_when_postgres_init_fails_strict(
    monkeypatch,
):
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.setenv("ORDER_SUBMISSION_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("ORDER_SUBMISSION_OUTBOX_BACKEND", "postgres")
    monkeypatch.setenv("ORDER_SUBMISSION_OUTBOX_REQUIRED", "true")
    monkeypatch.setattr(
        outbox_mod,
        "get_settings",
        lambda: SimpleNamespace(control_plane_backend="postgres"),
    )
    monkeypatch.setattr(
        outbox_mod,
        "get_control_plane_dsn",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("broken dsn")),
    )

    with pytest.raises(RuntimeError, match="backend=postgres"):
        outbox_mod.build_order_submission_outbox()


def test_build_order_submission_outbox_live_ignores_non_strict_override(
    monkeypatch,
):
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.setenv("ORDER_SUBMISSION_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("ORDER_SUBMISSION_OUTBOX_BACKEND", "postgres")
    monkeypatch.setenv("ORDER_SUBMISSION_OUTBOX_REQUIRED", "false")
    monkeypatch.setattr(
        outbox_mod,
        "get_settings",
        lambda: SimpleNamespace(control_plane_backend="postgres"),
    )
    monkeypatch.setattr(
        outbox_mod,
        "get_control_plane_dsn",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("broken dsn")),
    )

    with pytest.raises(RuntimeError, match="backend=postgres"):
        outbox_mod.build_order_submission_outbox()


def test_order_request_serialization_preserves_exit_account_context():
    request = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=1,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        purpose=OrderPurpose.EXIT,
        exit_reason="EOD",
        position_id="pos-1",
        contract_key_ref="NIFTY|2026-02-17|25750|CE|INTRADAY",
        strategy_id="ema20_strategy",
        account_id="acct-1",
    )

    payload = outbox_mod._serialize_order_request(request)
    restored = outbox_mod._deserialize_order_request(payload)

    assert restored.purpose == OrderPurpose.EXIT
    assert restored.exit_reason == "EOD"
    assert restored.position_id == "pos-1"
    assert restored.contract_key_ref == "NIFTY|2026-02-17|25750|CE|INTRADAY"
    assert restored.strategy_id == "ema20_strategy"
    assert restored.account_id == "acct-1"
