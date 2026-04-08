import logging
from types import SimpleNamespace

import pytest

from app.core import position_sync as position_sync_module
from app.core.position_sync import sync_positions_with_broker


class _EmptyOrderClient:
    def get_positions(self):
        return {"data": []}


class _LiveOrderClient:
    def get_positions(self):
        return {
            "data": [
                {
                    "netqty": 65,
                    "tradingsymbol": "LIVE24APR22000CE",
                    "symboltoken": "123",
                    "exchange": "NFO",
                    "avgprice": 100.0,
                    "ltp": 105.0,
                }
            ]
        }


class _RiskManager:
    def __init__(self, open_positions):
        self.open_positions = dict(open_positions)
        self.exit_calls = []
        self.entry_calls = []

    def _register_exit(self, label, price, qty):
        self.exit_calls.append((label, price, qty))

    def _register_entry(self, **kwargs):
        self.entry_calls.append(dict(kwargs))
        self.open_positions[kwargs["label"]] = {
            "side": kwargs["side"],
            "qty": kwargs["qty"],
            "entry_price": kwargs["price"],
        }

    def _infer_underlying(self, label):
        _ = label
        return "NIFTY"


@pytest.fixture(autouse=True)
def _clear_position_sync_cache():
    position_sync_module._LAST_GOOD_POSITIONS_BY_ACCOUNT.clear()
    position_sync_module._POSITION_SYNC_EVIDENCE_BY_ACCOUNT.clear()
    yield
    position_sync_module._LAST_GOOD_POSITIONS_BY_ACCOUNT.clear()
    position_sync_module._POSITION_SYNC_EVIDENCE_BY_ACCOUNT.clear()


def _patch_settings(monkeypatch, enabled: bool):
    monkeypatch.setattr(
        position_sync_module,
        "get_settings",
        lambda: SimpleNamespace(position_sync_stale_close_guard=enabled),
    )


def test_empty_broker_payload_does_not_stale_close_when_guard_enabled(monkeypatch, caplog):
    _patch_settings(monkeypatch, True)
    risk_manager = _RiskManager(
        {
            "STALE_LABEL": {
                "qty": 1,
                "side": "BUY",
                "entry_price": 100.0,
            }
        }
    )
    caplog.set_level(logging.WARNING)

    result = sync_positions_with_broker(
        order_client=_EmptyOrderClient(),
        risk_manager=risk_manager,
        instrument_meta={},
        broker_account_id="A1",
    )

    assert risk_manager.exit_calls == []
    assert result.closed == 0
    assert result.blocked is True
    assert result.blocked_reason == "empty_broker_payload_with_local_open_positions"
    assert "Skipping stale-close due to suspicious broker snapshot" in caplog.text


def test_healthy_snapshot_still_allows_stale_close(monkeypatch):
    _patch_settings(monkeypatch, True)
    risk_manager = _RiskManager(
        {
            "STALE_LABEL": {
                "qty": 1,
                "side": "BUY",
                "entry_price": 100.0,
            }
        }
    )
    instrument_meta = {
        "LIVE_LABEL": {
            "token": "123",
            "symbol": "LIVE24APR22000CE",
            "lot_size": 65,
            "exchange": "NFO",
        }
    }

    first = sync_positions_with_broker(
        order_client=_LiveOrderClient(),
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
        broker_account_id="A1",
    )
    second = sync_positions_with_broker(
        order_client=_LiveOrderClient(),
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
        broker_account_id="A1",
    )

    assert first.added == 0
    assert first.closed == 0
    assert first.reconciliation_state == "RECONCILING"
    assert second.added == 1
    assert second.closed == 1
    assert risk_manager.exit_calls == [("STALE_LABEL", 100.0, 1)]
