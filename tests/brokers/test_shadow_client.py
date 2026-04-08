from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.brokers.base import (
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.brokers.shadow_client import ShadowBrokerClient
from app.core.dashboard_bus import dashboard_bus
from app.tenants.models import BrokerAccountModel


def _account() -> BrokerAccountModel:
    return BrokerAccountModel(
        broker_account_id="shadow-A1",
        tenant_id="tenant-1",
        broker_type="angel",
        display_name="shadow",
        client_code="client",
        secret_ref="secret",
    )


def _set_market(label: str, symbol: str, price: float) -> None:
    dashboard_bus.set_instrument_meta(
        {
            label: {
                "label": label,
                "symbol": symbol,
                "token": "tok-1",
                "lot_size": 1,
            }
        }
    )
    dashboard_bus.record_tick(label, price, datetime.now(timezone.utc))


@pytest.mark.anyio
async def test_shadow_client_fills_with_ltp_slippage_and_side_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_FILL_LATENCY_MS", "0")
    monkeypatch.setenv("SHADOW_SLIPPAGE_BPS", "10")
    monkeypatch.setenv("SHADOW_SLIPPAGE_ABS", "0")
    monkeypatch.setenv("SHADOW_SLIPPAGE_MIN_TICKS", "0")
    monkeypatch.setenv("SHADOW_TICK_SIZE", "0.05")
    monkeypatch.setenv("SHADOW_PARTIAL_FILL_PROB", "0")

    symbol = "SYM_SHADOW_A"
    label = "SYM_SHADOW_A_LABEL"
    _set_market(label, symbol, 100.0)

    client = ShadowBrokerClient(_account())
    buy = await client.place_order(
        OrderRequest(
            symbol=symbol,
            quantity=2,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            symbol_token="tok-1",
        )
    )
    sell = await client.place_order(
        OrderRequest(
            symbol=symbol,
            quantity=2,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            symbol_token="tok-1",
        )
    )

    assert buy.status == "FILLED"
    assert buy.virtual is True
    assert buy.execution_mode == "SHADOW"
    assert round(float(buy.average_price or 0.0), 2) == 100.10
    assert sell.status == "FILLED"
    assert round(float(sell.average_price or 0.0), 2) == 99.90


@pytest.mark.anyio
async def test_shadow_client_supports_partial_fill_probability(monkeypatch, tmp_path):
    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_FILL_LATENCY_MS", "0")
    monkeypatch.setenv("SHADOW_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("SHADOW_SLIPPAGE_MIN_TICKS", "0")
    monkeypatch.setenv("SHADOW_PARTIAL_FILL_PROB", "1")
    monkeypatch.setenv("SHADOW_RNG_SEED", "42")

    symbol = "SYM_SHADOW_B"
    label = "SYM_SHADOW_B_LABEL"
    _set_market(label, symbol, 50.0)

    client = ShadowBrokerClient(_account())
    response = await client.place_order(
        OrderRequest(
            symbol=symbol,
            quantity=10,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            symbol_token="tok-1",
        )
    )

    assert response.status == "PARTIALLY_FILLED"
    assert response.filled_quantity == 5


@pytest.mark.anyio
async def test_shadow_client_rejects_when_ltp_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_FILL_LATENCY_MS", "0")
    monkeypatch.setenv("SHADOW_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("SHADOW_PARTIAL_FILL_PROB", "0")

    client = ShadowBrokerClient(_account())
    response = await client.place_order(
        OrderRequest(
            symbol="NO_LTP",
            quantity=1,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
        )
    )
    assert response.status == "REJECTED"
    assert "ltp_unavailable" in response.message


@pytest.mark.anyio
async def test_shadow_client_rejects_stale_ltp(monkeypatch, tmp_path):
    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_FILL_LATENCY_MS", "0")
    monkeypatch.setenv("SHADOW_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("SHADOW_PARTIAL_FILL_PROB", "0")
    monkeypatch.setenv("SHADOW_MAX_LTP_AGE_SECONDS", "1")

    label = "STALE_LTP_LABEL"
    symbol = "STALE_LTP_SYMBOL"
    dashboard_bus.set_instrument_meta(
        {label: {"label": label, "symbol": symbol, "token": "tok-2", "lot_size": 1}}
    )
    dashboard_bus.record_tick(
        label,
        200.0,
        datetime.now(timezone.utc) - timedelta(days=2),
    )

    client = ShadowBrokerClient(_account())
    response = await client.place_order(
        OrderRequest(
            symbol=symbol,
            quantity=1,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            symbol_token="tok-2",
        )
    )
    assert response.status == "REJECTED"
    assert "ltp_unavailable" in response.message


@pytest.mark.anyio
async def test_shadow_client_restores_persisted_state(monkeypatch, tmp_path):
    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_FILL_LATENCY_MS", "0")
    monkeypatch.setenv("SHADOW_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("SHADOW_PARTIAL_FILL_PROB", "0")

    symbol = "SYM_SHADOW_C"
    label = "SYM_SHADOW_C_LABEL"
    _set_market(label, symbol, 75.0)

    first = ShadowBrokerClient(_account())
    response = await first.place_order(
        OrderRequest(
            symbol=symbol,
            quantity=3,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            symbol_token="tok-1",
        )
    )
    assert response.status == "FILLED"

    second = ShadowBrokerClient(_account())
    orders = await second.get_orders()
    positions_result = await second.get_positions()

    assert len(orders) >= 1
    assert positions_result.positions is not None
    assert any(str(p.symbol) == symbol for p in positions_result.positions)
