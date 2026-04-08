import pytest

from app.brokers.base import OrderResponse
from app.strategies.ce_orb import CeOrbStrategy


def test_ce_orb_place_order_includes_exchange_and_token(monkeypatch):
    captured = {}

    def fake_bridge(**kwargs):
        captured["order_req"] = kwargs["order_req"]
        return OrderResponse(broker_order_id="ok", status="ACCEPTED", message="ok")

    monkeypatch.setattr(
        "app.strategies.ce_orb.place_order_via_bridge",
        fake_bridge,
    )

    label = "NIFTY24DEC25000CE"
    instrument_meta = {
        label: {
            "symbol": label,
            "exchange": "NFO",
            "token": "78910",
            "underlying": "NIFTY",
        }
    }

    strat = CeOrbStrategy(
        instrument_meta=instrument_meta,
        order_client=None,
        risk_manager=None,
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
    )

    strat._place_order(label=label, side="BUY", qty=50, price=100.0, tag="TEST")

    req = captured.get("order_req")
    assert req is not None
    assert req.exchange == "NFO"
    assert req.symbol_token == "78910"
    assert req.symbol == label
