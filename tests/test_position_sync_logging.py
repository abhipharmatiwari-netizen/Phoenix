from __future__ import annotations

import logging

import pytest

from app.core.position_sync import sync_positions_with_broker
import app.core.position_sync as position_sync
from app.core.dashboard_bus import DashboardBus


@pytest.fixture(autouse=True)
def _clear_position_sync_state():
    position_sync._LAST_GOOD_POSITIONS_BY_ACCOUNT.clear()
    position_sync._POSITION_SYNC_EVIDENCE_BY_ACCOUNT.clear()
    yield
    position_sync._LAST_GOOD_POSITIONS_BY_ACCOUNT.clear()
    position_sync._POSITION_SYNC_EVIDENCE_BY_ACCOUNT.clear()


class _OrderClient:
    def __init__(self, payload, account_id: str = "acct-default"):
        self._payload = payload
        self.broker_account_id = account_id

    def get_positions(self):
        return self._payload


class _FailingOrderClient:
    def __init__(self, exc: Exception, account_id: str):
        self._exc = exc
        self.broker_account_id = account_id

    def get_positions(self):
        raise self._exc


class _RiskManager:
    def __init__(self):
        self.open_positions = {}
        self.closed_positions = []
        self.update_calls = []
        self.pending_close_outcomes = {}
        self.kill_switch_activated = False
        self.suppressed_labels = set()

    def _infer_underlying(self, label):
        return label.split("_", 1)[0]

    def _register_entry(
        self,
        *,
        label,
        side,
        qty,
        price,
        template_name,
        role,
        underlying,
        strategy_name,
        reason,
        ml_info,
        strategy_context=None,
    ):
        self.open_positions[label] = {
            "side": side,
            "qty": qty,
            "entry_price": price,
            "template_name": template_name,
            "underlying": underlying,
            "strategy_name": strategy_name,
            "reason": reason,
            "role": role,
            "ml_info": ml_info,
        }
        if strategy_context is not None:
            self.open_positions[label]["strategy_context"] = dict(strategy_context)

    def _normalize_qty_to_units(self, _label, qty):
        return qty

    def update_position_from_broker(
        self,
        *,
        label,
        side,
        qty,
        entry_price,
        exchange=None,
        symboltoken=None,
        tradingsymbol=None,
        template_name=None,
        strategy_name=None,
        strategy_context=None,
    ):
        existing = dict(self.open_positions.get(label, {}))
        existing["side"] = side
        existing["qty"] = qty
        existing["entry_price"] = entry_price
        if template_name is not None:
            existing["template_name"] = template_name
        if exchange is not None:
            existing["exchange"] = exchange
        if symboltoken is not None:
            existing["symboltoken"] = symboltoken
        if tradingsymbol is not None:
            existing["tradingsymbol"] = tradingsymbol
        if strategy_name is not None:
            existing["strategy_name"] = strategy_name
        if strategy_context is not None:
            existing["strategy_context"] = dict(strategy_context)
        self.open_positions[label] = existing
        self.update_calls.append(dict(existing))

    def _persist_state(self):
        return None

    def _register_exit(self, label, exit_price, qty):
        self.closed_positions.append((label, exit_price, qty))

    def should_suppress_broker_sync_entry(self, label):
        return self.kill_switch_activated or label in self.suppressed_labels

    def reconcile_pending_closing_orders_for_label(self, label):
        outcome = dict(self.pending_close_outcomes.get(label, {}))
        if outcome.get("closed"):
            exit_price = float(outcome.get("exit_price", 0.0))
            qty = int(outcome.get("qty", 0) or 0)
            self.closed_positions.append((label, exit_price, qty))
            self.open_positions.pop(label, None)
        return outcome


def _summary_records(caplog):
    return [
        rec
        for rec in caplog.records
        if "[POS_SYNC] Summary:" in rec.getMessage()
    ]


def test_noop_summary_logs_debug_by_default(caplog, monkeypatch):
    monkeypatch.delenv("POSITION_SYNC_LOG_NOOP_SUMMARY", raising=False)
    caplog.set_level(logging.DEBUG)

    order_client = _OrderClient({"data": []})
    risk_manager = _RiskManager()
    instrument_meta = {}

    sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
    )

    summary = _summary_records(caplog)
    assert len(summary) == 1
    assert summary[0].levelno == logging.DEBUG


def test_noop_summary_logs_info_when_env_enabled(caplog, monkeypatch):
    monkeypatch.setenv("POSITION_SYNC_LOG_NOOP_SUMMARY", "true")
    caplog.set_level(logging.DEBUG)

    order_client = _OrderClient({"data": []})
    risk_manager = _RiskManager()
    instrument_meta = {}

    sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
    )

    summary = _summary_records(caplog)
    assert len(summary) == 1
    assert summary[0].levelno == logging.INFO


def test_actionable_summary_logs_info(caplog, monkeypatch):
    monkeypatch.delenv("POSITION_SYNC_LOG_NOOP_SUMMARY", raising=False)
    caplog.set_level(logging.DEBUG)

    order_client = _OrderClient(
        {
            "data": [
                {
                    "symboltoken": "12345",
                    "tradingsymbol": "NIFTY17FEB2625750CE",
                    "netqty": "65",
                    "avgprice": "100.0",
                    "ltp": "101.0",
                    "exchange": "NFO",
                }
            ]
        }
    )
    risk_manager = _RiskManager()
    instrument_meta = {
        "NIFTY_ATM_CE_25750": {
            "token": "12345",
            "symbol": "NIFTY17FEB2625750CE",
            "lot_size": 65,
            "exchange": "NFO",
        }
    }

    result = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
    )

    assert result.added == 0
    assert result.reconciliation_state == "RECONCILING"
    summary = _summary_records(caplog)
    assert len(summary) == 1
    assert summary[0].levelno == logging.INFO


def test_cached_snapshot_is_isolated_per_account(monkeypatch):
    monkeypatch.setenv("POSITION_SYNC_LAST_GOOD_TTL_SECONDS", "180")
    with position_sync._LAST_GOOD_POSITIONS_LOCK:
        position_sync._LAST_GOOD_POSITIONS_BY_ACCOUNT.clear()

    instrument_meta = {
        "NIFTY_ATM_CE_25750": {
            "token": "12345",
            "symbol": "NIFTY17FEB2625750CE",
            "lot_size": 65,
            "exchange": "NFO",
        }
    }

    # Account A receives a good snapshot and seeds the cache.
    ok_client = _OrderClient(
        {
            "data": [
                {
                    "symboltoken": "12345",
                    "tradingsymbol": "NIFTY17FEB2625750CE",
                    "netqty": "65",
                    "avgprice": "100.0",
                    "ltp": "101.0",
                    "exchange": "NFO",
                }
            ]
        },
        account_id="acct-A",
    )
    risk_a = _RiskManager()
    ok_result = sync_positions_with_broker(
        order_client=ok_client,
        risk_manager=risk_a,
        instrument_meta=instrument_meta,
    )
    assert ok_result.added == 0
    assert ok_result.reconciliation_state == "RECONCILING"
    assert ok_result.used_cached_snapshot is False

    # Account B fails fetch; it must NOT use account A cached snapshot.
    fail_client = _FailingOrderClient(RuntimeError("broker down"), account_id="acct-B")
    risk_b = _RiskManager()
    fail_result = sync_positions_with_broker(
        order_client=fail_client,
        risk_manager=risk_b,
        instrument_meta=instrument_meta,
    )
    assert fail_result.used_cached_snapshot is False
    assert fail_result.added == 0
    assert risk_b.open_positions == {}


def test_existing_entry_price_is_not_overwritten_by_ltp_when_avg_missing():
    order_client = _OrderClient(
        {
            "data": [
                {
                    "symboltoken": "65432",
                    "tradingsymbol": "NATURALGAS26MAR26FUT",
                    "netqty": "-1250",
                    "avgprice": None,
                    "ltp": "307.5",
                    "exchange": "MCX",
                }
            ]
        }
    )
    risk_manager = _RiskManager()
    risk_manager.open_positions["NG_FUT"] = {
        "side": "SELL",
        "qty": 1,
        "entry_price": 313.4,
        "template_name": "BROKER_SYNC",
        "underlying": "NG",
    }
    instrument_meta = {
        "NG_FUT": {
            "token": "65432",
            "symbol": "NATURALGAS26MAR26FUT",
            "lot_size": 1250,
            "exchange": "MCX",
        }
    }

    result = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
    )

    assert result.updated == 0
    assert risk_manager.open_positions["NG_FUT"]["entry_price"] == 313.4


def test_sync_uses_directional_average_price_when_avgprice_missing():
    order_client = _OrderClient(
        {
            "data": [
                {
                    "symboltoken": "65432",
                    "tradingsymbol": "NATURALGAS26MAR26FUT",
                    "netqty": "-1250",
                    "avgprice": None,
                    "sellavgprice": "313.4",
                    "ltp": "309.8",
                    "exchange": "MCX",
                }
            ]
        }
    )
    risk_manager = _RiskManager()
    risk_manager.open_positions["NG_FUT"] = {
        "side": "SELL",
        "qty": 1,
        "entry_price": 307.5,
        "template_name": "BROKER_SYNC",
        "underlying": "NG",
    }
    instrument_meta = {
        "NG_FUT": {
            "token": "65432",
            "symbol": "NATURALGAS26MAR26FUT",
            "lot_size": 1250,
            "exchange": "MCX",
        }
    }

    result = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
    )

    assert result.updated == 1
    assert risk_manager.open_positions["NG_FUT"]["entry_price"] == 313.4


def test_new_broker_synced_position_falls_back_to_ltp_when_avg_missing():
    order_client = _OrderClient(
        {
            "data": [
                {
                    "symboltoken": "65432",
                    "tradingsymbol": "NATURALGAS26MAR26FUT",
                    "netqty": "-1250",
                    "avgprice": None,
                    "ltp": "309.8",
                    "exchange": "MCX",
                }
            ]
        }
    )
    risk_manager = _RiskManager()
    instrument_meta = {
        "NG_FUT": {
            "token": "65432",
            "symbol": "NATURALGAS26MAR26FUT",
            "lot_size": 1250,
            "exchange": "MCX",
        }
    }

    first = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
    )
    second = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
    )

    assert first.added == 0
    assert first.reconciliation_state == "RECONCILING"
    assert second.added == 1
    assert risk_manager.open_positions["NG_FUT"]["entry_price"] == 309.8


def test_new_broker_synced_position_is_suppressed_during_kill_switch(caplog):
    order_client = _OrderClient(
        {
            "data": [
                {
                    "symboltoken": "65432",
                    "tradingsymbol": "NATURALGAS26MAR26FUT",
                    "netqty": "-1250",
                    "avgprice": "309.8",
                    "ltp": "309.8",
                    "exchange": "MCX",
                }
            ]
        }
    )
    risk_manager = _RiskManager()
    risk_manager.kill_switch_activated = True
    instrument_meta = {
        "NG_FUT": {
            "token": "65432",
            "symbol": "NATURALGAS26MAR26FUT",
            "lot_size": 1250,
            "exchange": "MCX",
        }
    }
    caplog.set_level(logging.WARNING)

    result = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
    )

    assert result.added == 0
    assert risk_manager.open_positions == {}
    assert "event_type=BROKER_SYNC_SUPPRESSED" in caplog.text


def test_new_broker_synced_position_is_suppressed_while_forced_exit_is_in_flight():
    order_client = _OrderClient(
        {
            "data": [
                {
                    "symboltoken": "65432",
                    "tradingsymbol": "NATURALGAS26MAR26FUT",
                    "netqty": "-1250",
                    "avgprice": "309.8",
                    "ltp": "309.8",
                    "exchange": "MCX",
                }
            ]
        }
    )
    risk_manager = _RiskManager()
    risk_manager.suppressed_labels.add("NG_FUT")
    instrument_meta = {
        "NG_FUT": {
            "token": "65432",
            "symbol": "NATURALGAS26MAR26FUT",
            "lot_size": 1250,
            "exchange": "MCX",
        }
    }

    result = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
    )

    assert result.added == 0
    assert risk_manager.open_positions == {}


def test_existing_position_mapping_survives_missing_instrument_meta():
    order_client = _OrderClient(
        {
            "data": [
                {
                    "symboltoken": "505131",
                    "tradingsymbol": "NATURALGAS24MAR26310CE",
                    "netqty": "-1250",
                    "avgprice": "26.6",
                    "ltp": "27.75",
                    "exchange": "MCX",
                }
            ]
        }
    )
    risk_manager = _RiskManager()
    risk_manager.open_positions["NG_ATM_CE_310"] = {
        "side": "SELL",
        "qty": 1,
        "entry_price": 26.6,
        "lot_size": 1250,
        "symboltoken": "505131",
        "tradingsymbol": "NATURALGAS24MAR26310CE",
        "template_name": "BROKER_SYNC",
        "underlying": "NG",
    }

    result = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta={},
    )

    assert result.closed == 0
    assert result.errors == []
    assert risk_manager.closed_positions == []


def test_existing_risk_label_wins_over_refreshed_meta_label_for_same_contract():
    order_client = _OrderClient(
        {
            "data": [
                {
                    "symboltoken": "505129",
                    "tradingsymbol": "NATURALGAS24MAR26285CE",
                    "netqty": "-1250",
                    "avgprice": "18.70",
                    "ltp": "19.20",
                    "exchange": "MCX",
                }
            ]
        }
    )
    risk_manager = _RiskManager()
    risk_manager.open_positions["NG_ATM_CE_285"] = {
        "side": "SELL",
        "qty": 1,
        "entry_price": 18.7,
        "lot_size": 1250,
        "symboltoken": "505129",
        "tradingsymbol": "NATURALGAS24MAR26285CE",
        "template_name": "ema20-trend",
        "underlying": "NG",
        "strategy_name": "ema20-trend",
    }
    instrument_meta = {
        "NG_OTM_CE_285": {
            "token": "505129",
            "symbol": "NATURALGAS24MAR26285CE",
            "lot_size": 1250,
            "exchange": "MCX",
        }
    }

    result = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
    )

    assert result.added == 0
    assert result.updated == 0
    assert result.closed == 0
    assert "NG_ATM_CE_285" in risk_manager.open_positions
    assert "NG_OTM_CE_285" not in risk_manager.open_positions
    assert risk_manager.closed_positions == []


def test_same_contract_duplicate_labels_are_not_stale_closed():
    order_client = _OrderClient(
        {
            "data": [
                {
                    "symboltoken": "505129",
                    "tradingsymbol": "NATURALGAS24MAR26285CE",
                    "netqty": "-1250",
                    "avgprice": "18.70",
                    "ltp": "19.20",
                    "exchange": "MCX",
                }
            ]
        }
    )
    risk_manager = _RiskManager()
    risk_manager.open_positions["NG_ATM_CE_285"] = {
        "side": "SELL",
        "qty": 1,
        "entry_price": 18.7,
        "lot_size": 1250,
        "symboltoken": "505129",
        "tradingsymbol": "NATURALGAS24MAR26285CE",
        "template_name": "BROKER_SYNC",
        "underlying": "NG",
        "strategy_name": "BROKER_SYNC",
    }
    risk_manager.open_positions["NG_OTM_CE_285"] = {
        "side": "SELL",
        "qty": 1,
        "entry_price": 18.7,
        "lot_size": 1250,
        "symboltoken": "505129",
        "tradingsymbol": "NATURALGAS24MAR26285CE",
        "template_name": "BROKER_SYNC",
        "underlying": "NG",
        "strategy_name": "BROKER_SYNC",
    }
    instrument_meta = {
        "NG_OTM_CE_285": {
            "token": "505129",
            "symbol": "NATURALGAS24MAR26285CE",
            "lot_size": 1250,
            "exchange": "MCX",
        }
    }

    result = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
    )

    assert result.closed == 0
    assert risk_manager.closed_positions == []


def test_stale_close_prefers_pending_broker_exit_reconciliation_over_ltp():
    order_client = _OrderClient({"data": []})
    risk_manager = _RiskManager()
    risk_manager.open_positions["NIFTY_ATM_CE_23150"] = {
        "side": "BUY",
        "qty": 1,
        "entry_price": 197.4,
        "lot_size": 65,
        "symboltoken": "126001",
        "tradingsymbol": "NIFTY17MAR2623150CE",
        "template_name": "ema20-trend",
        "underlying": "NIFTY",
        "strategy_name": "ema20-trend",
    }
    risk_manager.pending_close_outcomes["NIFTY_ATM_CE_23150"] = {
        "closed": True,
        "pending": False,
        "exit_price": 201.2,
        "qty": 1,
    }

    first = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta={},
        ltp_lookup={"NIFTY_ATM_CE_23150": 185.7},
    )
    second = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta={},
        ltp_lookup={"NIFTY_ATM_CE_23150": 185.7},
    )

    assert first.closed == 0
    assert first.reconciliation_state == "RECONCILING"
    assert second.closed == 1
    assert risk_manager.closed_positions == [("NIFTY_ATM_CE_23150", 201.2, 1)]


def test_stale_close_is_deferred_while_pending_broker_exit_is_unresolved():
    order_client = _OrderClient({"data": []})
    risk_manager = _RiskManager()
    risk_manager.open_positions["NIFTY_ATM_CE_23150"] = {
        "side": "BUY",
        "qty": 1,
        "entry_price": 197.4,
        "lot_size": 65,
        "symboltoken": "126001",
        "tradingsymbol": "NIFTY17MAR2623150CE",
        "template_name": "ema20-trend",
        "underlying": "NIFTY",
        "strategy_name": "ema20-trend",
    }
    risk_manager.pending_close_outcomes["NIFTY_ATM_CE_23150"] = {
        "closed": False,
        "pending": True,
    }

    result = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta={},
        ltp_lookup={"NIFTY_ATM_CE_23150": 185.7},
    )

    assert result.closed == 0
    assert risk_manager.closed_positions == []
    assert "NIFTY_ATM_CE_23150" in risk_manager.open_positions


def test_new_broker_synced_position_uses_recent_strategy_context():
    order_client = _OrderClient(
        {
            "data": [
                {
                    "symboltoken": "505131",
                    "tradingsymbol": "NATURALGAS24MAR26310CE",
                    "netqty": "-1250",
                    "avgprice": "29.55",
                    "ltp": "28.95",
                    "exchange": "MCX",
                }
            ]
        }
    )
    risk_manager = _RiskManager()
    instrument_meta = {
        "NG_ATM_CE_310": {
            "token": "505131",
            "symbol": "NATURALGAS24MAR26310CE",
            "lot_size": 1250,
            "exchange": "MCX",
        }
    }

    result = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
        entry_submission_lookup=lambda **_kwargs: {
            "strategy_name": "ema20-trend",
            "strategy_context": {"tp_pct": 0.08, "sl_pct": 0.15},
            "position_label": "NG_ATM_CE_310",
        },
    )

    assert result.added == 1
    assert risk_manager.open_positions["NG_ATM_CE_310"]["strategy_name"] == "ema20-trend"
    assert risk_manager.open_positions["NG_ATM_CE_310"]["strategy_context"] == {
        "tp_pct": 0.08,
        "sl_pct": 0.15,
    }


def test_existing_broker_sync_position_is_enriched_without_price_change():
    order_client = _OrderClient(
        {
            "data": [
                {
                    "symboltoken": "505131",
                    "tradingsymbol": "NATURALGAS24MAR26310CE",
                    "netqty": "-1250",
                    "avgprice": "29.55",
                    "ltp": "28.95",
                    "exchange": "MCX",
                }
            ]
        }
    )
    risk_manager = _RiskManager()
    risk_manager.open_positions["NG_ATM_CE_310"] = {
        "side": "SELL",
        "qty": 1,
        "entry_price": 29.55,
        "template_name": "BROKER_SYNC",
        "underlying": "NG",
        "strategy_name": "BROKER_SYNC",
        "symboltoken": "505131",
        "tradingsymbol": "NATURALGAS24MAR26310CE",
    }
    instrument_meta = {
        "NG_ATM_CE_310": {
            "token": "505131",
            "symbol": "NATURALGAS24MAR26310CE",
            "lot_size": 1250,
            "exchange": "MCX",
        }
    }

    result = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
        entry_submission_lookup=lambda **_kwargs: {
            "strategy_name": "ema20-trend",
            "strategy_context": {"tp_pct": 0.08, "sl_pct": 0.15},
            "position_label": "NG_ATM_CE_310",
        },
    )

    assert result.updated == 1
    assert risk_manager.open_positions["NG_ATM_CE_310"]["strategy_name"] == "ema20-trend"
    assert risk_manager.open_positions["NG_ATM_CE_310"]["strategy_context"] == {
        "tp_pct": 0.08,
        "sl_pct": 0.15,
    }


def test_recent_strategy_context_uses_explicit_broker_account_id():
    order_client = _OrderClient(
        {
            "data": [
                {
                    "symboltoken": "505131",
                    "tradingsymbol": "NATURALGAS24MAR26310CE",
                    "netqty": "-1250",
                    "avgprice": "27.15",
                    "ltp": "26.80",
                    "exchange": "MCX",
                }
            ]
        },
        account_id="legacy-default",
    )
    risk_manager = _RiskManager()
    instrument_meta = {
        "NG_ATM_CE_310": {
            "token": "505131",
            "symbol": "NATURALGAS24MAR26310CE",
            "lot_size": 1250,
            "exchange": "MCX",
        }
    }
    lookup_calls = []

    def _lookup(**kwargs):
        lookup_calls.append(dict(kwargs))
        if kwargs.get("broker_account_id") != "A1":
            return None
        return {
            "strategy_name": "ema20-trend",
            "strategy_context": {"tp_pct": 0.08, "sl_pct": 0.15},
            "position_label": "NG_ATM_CE_310",
        }

    result = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
        broker_account_id="A1",
        entry_submission_lookup=_lookup,
    )

    assert result.added == 1
    assert lookup_calls == [
        {
            "broker_account_id": "A1",
            "label": "NG_ATM_CE_310",
            "symbol": "NATURALGAS24MAR26310CE",
            "side": "SELL",
        }
    ]
    assert risk_manager.open_positions["NG_ATM_CE_310"]["strategy_name"] == "ema20-trend"
    assert risk_manager.open_positions["NG_ATM_CE_310"]["strategy_context"] == {
        "tp_pct": 0.08,
        "sl_pct": 0.15,
    }


def test_unmapped_broker_position_is_adopted_and_marked_to_market(monkeypatch):
    order_client = _OrderClient(
        {
            "data": [
                {
                    "symboltoken": "505130",
                    "tradingsymbol": "NATURALGAS24MAR26305CE",
                    "netqty": "-1250",
                    "avgprice": "26.20",
                    "ltp": "25.90",
                    "exchange": "MCX",
                }
            ]
        },
        account_id="A1",
    )
    risk_manager = _RiskManager()
    instrument_meta = {}
    bus = DashboardBus()
    monkeypatch.setattr(position_sync, "dashboard_bus", bus)

    first = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
    )
    second = sync_positions_with_broker(
        order_client=order_client,
        risk_manager=risk_manager,
        instrument_meta=instrument_meta,
    )

    assert first.added == 0
    assert first.reconciliation_state == "RECONCILING"
    assert second.added == 1
    assert second.errors == []
    assert "NATURALGAS24MAR26305CE" in risk_manager.open_positions
    assert risk_manager.open_positions["NATURALGAS24MAR26305CE"]["symboltoken"] == "505130"
    assert risk_manager.open_positions["NATURALGAS24MAR26305CE"]["tradingsymbol"] == "NATURALGAS24MAR26305CE"
    assert instrument_meta["NATURALGAS24MAR26305CE"]["lot_size"] == 1250.0
    assert bus.get_last_price_for_instrument(symbol="NATURALGAS24MAR26305CE") == 25.9
    assert bus.get_last_price_for_instrument(token="505130") == 25.9
