import importlib
from types import SimpleNamespace

import pytest

from app.brokers.base import OrderPurpose, OrderSide, OrderType
from app.core.order_client import AngelHTTPBlockedError


class _ImmediateThread:
    def __init__(self, target, daemon=True, name=None):
        self._target = target

    def start(self):
        self._target()


class _StubOrderClient:
    def __init__(self):
        self.orders = []

    def place_order(self, request):
        self.orders.append(request)
        return SimpleNamespace(status="success")


class _ConfirmedOrderClient:
    def __init__(self, *, stoploss_exc=None):
        self.calls = []
        self.stoploss_exc = stoploss_exc

    def place_order_confirmed(self, **kwargs):
        self.calls.append(dict(kwargs))
        if (
            self.stoploss_exc is not None
            and str(kwargs.get("tag") or "").startswith("STOPLOSS")
            and kwargs.get("ordertype") == "SLM"
        ):
            raise self.stoploss_exc
        return {
            "terminal_status": "PENDING",
            "order_id": f"OID-{len(self.calls)}",
            "place_order": {"status": True, "message": "SUCCESS"},
            "order_row": {
                "ordertype": kwargs.get("ordertype"),
                "price": kwargs.get("price"),
                "triggerprice": kwargs.get("trigger_price"),
            },
        }


class _ImmediateFillConfirmedOrderClient:
    def __init__(self):
        self.calls = []

    def place_order_confirmed(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {
            "terminal_status": "COMPLETE",
            "order_id": f"OID-{len(self.calls)}",
            "place_order": {"status": True, "message": "SUCCESS"},
            "order_row": {
                "orderstatus": "complete",
                "averageprice": kwargs.get("price") or kwargs.get("trigger_price") or 0.0,
                "price": kwargs.get("price"),
                "triggerprice": kwargs.get("trigger_price"),
            },
        }


class _SuppressStoplossAfterTargetConfirmedOrderClient:
    def __init__(self, risk_manager, label):
        self.calls = []
        self._risk_manager = risk_manager
        self._label = label

    def place_order_confirmed(self, **kwargs):
        self.calls.append(dict(kwargs))
        if str(kwargs.get("tag") or "").startswith("TARGET"):
            self._risk_manager._mark_forced_exit_suppression(self._label)
        return {
            "terminal_status": "PENDING",
            "order_id": f"OID-{len(self.calls)}",
            "place_order": {"status": True, "message": "SUCCESS"},
            "order_row": {
                "ordertype": kwargs.get("ordertype"),
                "price": kwargs.get("price"),
                "triggerprice": kwargs.get("trigger_price"),
            },
        }


def test_bracket_orders_place_target_and_stoploss(monkeypatch, tmp_path):
    mod = importlib.import_module("app.core.risk_manager")
    monkeypatch.setattr(mod.threading, "Thread", _ImmediateThread)

    order_client = _StubOrderClient()
    risk_manager = mod.RiskManager(
        instrument_meta={
            "NIFTY24MAYFUT": {
                "exchange": "NSE",
                "token": "123456",
                "symbol": "NIFTY24MAYFUT",
            }
        },
        order_client=order_client,
        state_path=str(tmp_path / "risk_state.json"),
    )

    risk_manager._place_bracket_orders_async(
        label="NIFTY24MAYFUT",
        side="BUY",
        qty=2,
        entry_price=100.0,
        strategy_name="TEST",
        strategy_context={"target_pct": 2.0, "sl_pct": 1.0},
    )

    assert len(order_client.orders) == 2
    target_order = next(o for o in order_client.orders if o.tag.startswith("TARGET"))
    stoploss_order = next(o for o in order_client.orders if o.tag.startswith("STOPLOSS"))

    assert target_order.side == OrderSide.SELL
    assert target_order.order_type == OrderType.LIMIT
    assert target_order.limit_price == pytest.approx(102.0)
    assert target_order.purpose == OrderPurpose.EXIT

    assert stoploss_order.side == OrderSide.SELL
    assert stoploss_order.order_type == OrderType.SLM
    assert stoploss_order.limit_price is None
    assert stoploss_order.stop_price == pytest.approx(99.0)
    assert stoploss_order.purpose == OrderPurpose.EXIT


def test_bracket_orders_short_side_uses_tp_pct_alias_and_tick_size(monkeypatch, tmp_path):
    mod = importlib.import_module("app.core.risk_manager")
    monkeypatch.setattr(mod.threading, "Thread", _ImmediateThread)

    order_client = _StubOrderClient()
    risk_manager = mod.RiskManager(
        instrument_meta={
            "NG_ATM_CE_310": {
                "exchange": "MCX",
                "token": "505131",
                "symbol": "NATURALGAS24MAR26310CE",
                "lot_size": 1250,
                "tick_size": 0.05,
            }
        },
        order_client=order_client,
        state_path=str(tmp_path / "risk_state.json"),
    )

    risk_manager._place_bracket_orders_async(
        label="NG_ATM_CE_310",
        side="SELL",
        qty=1,
        entry_price=26.60,
        strategy_name="BROKER_SYNC",
        strategy_context={"tp_pct": 2.0, "sl_pct": 1.0},
    )

    assert len(order_client.orders) == 2
    target_order = next(o for o in order_client.orders if o.tag.startswith("TARGET"))
    stoploss_order = next(o for o in order_client.orders if o.tag.startswith("STOPLOSS"))

    assert target_order.side == OrderSide.BUY
    assert target_order.order_type == OrderType.LIMIT
    assert target_order.limit_price == pytest.approx(26.05)
    assert target_order.tick_size == pytest.approx(0.05)

    assert stoploss_order.side == OrderSide.BUY
    assert stoploss_order.order_type == OrderType.SLM
    assert stoploss_order.stop_price == pytest.approx(26.85)
    assert stoploss_order.tick_size == pytest.approx(0.05)


def test_bracket_orders_short_side_falls_back_to_env_percent_defaults(monkeypatch, tmp_path):
    mod = importlib.import_module("app.core.risk_manager")
    monkeypatch.setattr(mod.threading, "Thread", _ImmediateThread)
    monkeypatch.setenv("DEFAULT_PROFIT_TARGET_PCT", "0.50")
    monkeypatch.setenv("DEFAULT_STOPLOSS_PCT", "1.0")

    order_client = _StubOrderClient()
    risk_manager = mod.RiskManager(
        instrument_meta={
            "NG_ATM_CE_310": {
                "exchange": "MCX",
                "token": "505131",
                "symbol": "NATURALGAS24MAR26310CE",
                "lot_size": 1250,
                "tick_size": 0.05,
            }
        },
        order_client=order_client,
        state_path=str(tmp_path / "risk_state.json"),
    )

    risk_manager._place_bracket_orders_async(
        label="NG_ATM_CE_310",
        side="SELL",
        qty=1,
        entry_price=26.60,
        strategy_name="BROKER_SYNC",
        strategy_context=None,
    )

    target_order = next(o for o in order_client.orders if o.tag.startswith("TARGET"))
    stoploss_order = next(o for o in order_client.orders if o.tag.startswith("STOPLOSS"))

    assert target_order.limit_price == pytest.approx(26.45)
    assert stoploss_order.stop_price == pytest.approx(26.85)


def test_register_entry_skips_broker_brackets_for_strategy_managed_exits(
    monkeypatch,
    tmp_path,
):
    mod = importlib.import_module("app.core.risk_manager")
    monkeypatch.setattr(mod.threading, "Thread", _ImmediateThread)

    order_client = _StubOrderClient()
    risk_manager = mod.RiskManager(
        instrument_meta={
            "NG_ATM_CE_310": {
                "exchange": "MCX",
                "token": "505131",
                "symbol": "NATURALGAS24MAR26310CE",
                "lot_size": 1250,
                "tick_size": 0.05,
            }
        },
        order_client=order_client,
        state_path=str(tmp_path / "risk_state.json"),
    )

    risk_manager._register_entry(
        label="NG_ATM_CE_310",
        side="SELL",
        qty=1,
        price=26.60,
        template_name="ema20-trend",
        role=None,
        underlying="NG",
        strategy_name="ema20-trend",
        strategy_context={
            "tp_pct": 0.35,
            "sl_pct": 0.25,
            "managed_exit_mode": "strategy",
            "broker_brackets_enabled": False,
        },
    )

    assert order_client.orders == []


def test_register_entry_skips_broker_brackets_for_broker_sync_recovery(
    monkeypatch,
    tmp_path,
):
    mod = importlib.import_module("app.core.risk_manager")
    monkeypatch.setattr(mod.threading, "Thread", _ImmediateThread)

    order_client = _StubOrderClient()
    risk_manager = mod.RiskManager(
        instrument_meta={
            "NG_ATM_CE_310": {
                "exchange": "MCX",
                "token": "505131",
                "symbol": "NATURALGAS24MAR26310CE",
                "lot_size": 1250,
                "tick_size": 0.05,
            }
        },
        order_client=order_client,
        state_path=str(tmp_path / "risk_state.json"),
    )

    risk_manager._register_entry(
        label="NG_ATM_CE_310",
        side="SELL",
        qty=1,
        price=26.60,
        template_name="BROKER_SYNC",
        role=None,
        underlying="NG",
        strategy_name="BROKER_SYNC",
        reason="BROKER_SYNC",
        strategy_context={"tp_pct": 0.35, "sl_pct": 0.25},
    )

    assert order_client.orders == []


@pytest.mark.parametrize(
    ("entry_side", "label", "symbol", "entry_price", "expected_trigger", "expected_limit"),
    [
        ("SELL", "NG_ATM_CE_310", "NATURALGAS24MAR26310CE", 26.60, 26.85, 26.90),
        ("BUY", "NIFTY24MAYFUT", "NIFTY24MAYFUT", 100.0, 99.0, 98.95),
    ],
)
def test_bracket_stoploss_retries_invalid_slm_as_sl_with_tick_offset(
    monkeypatch,
    tmp_path,
    entry_side,
    label,
    symbol,
    entry_price,
    expected_trigger,
    expected_limit,
):
    mod = importlib.import_module("app.core.risk_manager")
    monkeypatch.setattr(mod.threading, "Thread", _ImmediateThread)

    order_client = _ConfirmedOrderClient(
        stoploss_exc=RuntimeError(
            "Order failed: {'data': None, 'error': {'errorCode': 'VND1001', 'message': 'Invalid value for field : ordertype'}}"
        )
    )
    risk_manager = mod.RiskManager(
        instrument_meta={
            label: {
                "exchange": "MCX",
                "token": "505131",
                "symbol": symbol,
                "lot_size": 1250,
                "tick_size": 0.05,
            }
        },
        order_client=order_client,
        state_path=str(tmp_path / "risk_state.json"),
    )

    risk_manager._place_bracket_orders_async(
        label=label,
        side=entry_side,
        qty=1,
        entry_price=entry_price,
        strategy_name="BROKER_SYNC",
        strategy_context={"tp_pct": 2.0, "sl_pct": 1.0},
    )

    assert [call["ordertype"] for call in order_client.calls] == ["LIMIT", "SLM", "SL"]
    fallback_call = order_client.calls[-1]
    assert fallback_call["trigger_price"] == pytest.approx(expected_trigger)
    assert fallback_call["price"] == pytest.approx(expected_limit)


def test_bracket_stoploss_does_not_blind_retry_non_ordertype_failures(
    monkeypatch,
    tmp_path,
):
    mod = importlib.import_module("app.core.risk_manager")
    monkeypatch.setattr(mod.threading, "Thread", _ImmediateThread)

    order_client = _ConfirmedOrderClient(
        stoploss_exc=RuntimeError(
            "Order failed: {'data': None, 'error': {'errorCode': 'AB9999', 'message': 'some other failure'}}"
        )
    )
    risk_manager = mod.RiskManager(
        instrument_meta={
            "NG_ATM_CE_310": {
                "exchange": "MCX",
                "token": "505131",
                "symbol": "NATURALGAS24MAR26310CE",
                "lot_size": 1250,
                "tick_size": 0.05,
            }
        },
        order_client=order_client,
        state_path=str(tmp_path / "risk_state.json"),
    )

    risk_manager._place_bracket_orders_async(
        label="NG_ATM_CE_310",
        side="SELL",
        qty=1,
        entry_price=26.60,
        strategy_name="BROKER_SYNC",
        strategy_context={"tp_pct": 2.0, "sl_pct": 1.0},
    )

    assert [call["ordertype"] for call in order_client.calls] == ["LIMIT", "SLM"]


def test_bracket_stoploss_logs_structured_broker_payload(
    monkeypatch,
    tmp_path,
    caplog,
):
    mod = importlib.import_module("app.core.risk_manager")
    monkeypatch.setattr(mod.threading, "Thread", _ImmediateThread)

    order_client = _ConfirmedOrderClient(
        stoploss_exc=AngelHTTPBlockedError(
            "HTML response",
            url="https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/placeOrder",
            status=400,
            body_head="{\"message\":\"invalid stoploss\"}",
            content_type="application/json",
        )
    )
    risk_manager = mod.RiskManager(
        instrument_meta={
            "NG_ATM_CE_310": {
                "exchange": "MCX",
                "token": "505131",
                "symbol": "NATURALGAS24MAR26310CE",
                "lot_size": 1250,
                "tick_size": 0.05,
            }
        },
        order_client=order_client,
        state_path=str(tmp_path / "risk_state.json"),
    )
    caplog.set_level("WARNING")

    risk_manager._place_bracket_orders_async(
        label="NG_ATM_CE_310",
        side="SELL",
        qty=1,
        entry_price=26.60,
        strategy_name="ema20-trend",
        strategy_context={"tp_pct": 2.0, "sl_pct": 1.0},
    )

    assert "event_type=BRACKET_STOPLOSS_ORDER_ERROR" in caplog.text
    assert 'broker_body={"message":"invalid stoploss"}' in caplog.text


def test_register_entry_skips_stoploss_when_forced_exit_activates_after_target(
    monkeypatch,
    tmp_path,
):
    mod = importlib.import_module("app.core.risk_manager")
    monkeypatch.setattr(mod.threading, "Thread", _ImmediateThread)

    risk_manager = mod.RiskManager(
        instrument_meta={
            "NIFTY24MAYFUT": {
                "exchange": "NSE",
                "token": "123456",
                "symbol": "NIFTY24MAYFUT",
                "lot_size": 1,
                "tick_size": 0.05,
            }
        },
        order_client=_StubOrderClient(),
        state_path=str(tmp_path / "risk_state.json"),
    )
    order_client = _SuppressStoplossAfterTargetConfirmedOrderClient(
        risk_manager,
        "NIFTY24MAYFUT",
    )
    risk_manager.order_client = order_client

    risk_manager._register_entry(
        label="NIFTY24MAYFUT",
        side="BUY",
        qty=1,
        price=100.0,
        template_name="ema20-trend",
        role=None,
        underlying="NIFTY",
        strategy_name="ema20-trend",
        reason="ENTRY",
        strategy_context={"tp_pct": 2.0, "sl_pct": 1.0},
    )

    assert [call["ordertype"] for call in order_client.calls] == ["LIMIT"]


def test_register_entry_tracks_pending_broker_bracket_exits(monkeypatch, tmp_path):
    mod = importlib.import_module("app.core.risk_manager")
    monkeypatch.setattr(mod.threading, "Thread", _ImmediateThread)

    order_client = _ConfirmedOrderClient()
    risk_manager = mod.RiskManager(
        instrument_meta={
            "NIFTY_ATM_CE_23150": {
                "exchange": "NFO",
                "token": "126001",
                "symbol": "NIFTY17MAR2623150CE",
                "lot_size": 65,
                "tick_size": 0.05,
                "underlying": "NIFTY",
            }
        },
        order_client=order_client,
        state_path=str(tmp_path / "risk_state.json"),
    )

    risk_manager._register_entry(
        label="NIFTY_ATM_CE_23150",
        side="BUY",
        qty=1,
        price=197.40,
        template_name="ema20-trend",
        role=None,
        underlying="NIFTY",
        strategy_name="ema20-trend",
        reason="ENTRY",
        ml_info=None,
        strategy_context={"tp_pct": 2.0, "sl_pct": 1.0},
    )

    assert set(risk_manager._pending_orders.keys()) == {"OID-1", "OID-2"}
    for pending in risk_manager._pending_orders.values():
        assert pending["label"] == "NIFTY_ATM_CE_23150"
        assert pending["qty"] == 1
        assert pending["is_closing"] is True


def test_register_entry_closes_position_when_bracket_target_fills_immediately(
    monkeypatch,
    tmp_path,
):
    mod = importlib.import_module("app.core.risk_manager")
    monkeypatch.setattr(mod.threading, "Thread", _ImmediateThread)

    order_client = _ImmediateFillConfirmedOrderClient()
    risk_manager = mod.RiskManager(
        instrument_meta={
            "NIFTY_ATM_CE_23150": {
                "exchange": "NFO",
                "token": "126001",
                "symbol": "NIFTY17MAR2623150CE",
                "lot_size": 65,
                "tick_size": 0.05,
                "underlying": "NIFTY",
            }
        },
        order_client=order_client,
        state_path=str(tmp_path / "risk_state.json"),
    )

    risk_manager._register_entry(
        label="NIFTY_ATM_CE_23150",
        side="BUY",
        qty=1,
        price=197.40,
        template_name="ema20-trend",
        role=None,
        underlying="NIFTY",
        strategy_name="ema20-trend",
        reason="ENTRY",
        ml_info=None,
        strategy_context={"tp_pct": 2.0, "sl_pct": 1.0},
    )

    assert order_client.calls[0]["ordertype"] == "LIMIT"
    assert len(order_client.calls) == 1
    assert "NIFTY_ATM_CE_23150" not in risk_manager.open_positions
    assert risk_manager.realized_pnl == pytest.approx(256.75)
