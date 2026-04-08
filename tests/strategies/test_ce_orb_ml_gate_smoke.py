from datetime import datetime, timezone

from app.strategies.ce_orb import CeOrbStrategy


class DummyMLGate:
    def evaluate_candidate(self, **kwargs):
        raise AssertionError("CE_ORB should not invoke ml_gate")


class DummyRiskManager:
    def __init__(self):
        self.trade_persister = None


class DummyCandle:
    def __init__(self, close: float):
        self.c = close
        self.h = close
        self.low = close
        self.start_ts = datetime.now(timezone.utc)
        self.end_ts = self.start_ts


def test_ce_orb_ignores_ml_gate_and_places_entry():
    strategy = CeOrbStrategy(
        instrument_meta={"NIFTY_CE_20000": {"underlying": "NIFTY", "kind": "CE", "lot_size": 65}},
        order_client=None,
        risk_manager=DummyRiskManager(),
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        params={"lots_per_trade": 1, "first_entry_time": "00:00", "last_entry_time": "23:59"},
        ml_gate=DummyMLGate(),
    )
    strategy.state.orb_high = 100.0
    strategy.state.orb_low = 99.0
    strategy.state.vwap_sum_pv = 100.0
    strategy.state.vwap_sum_v = 1.0
    strategy.last_price["NIFTY_CE_20000"] = 10.0
    calls = {"count": 0}

    class _Resp:
        status = "SUCCESS"
        message = ""

    def _fake_place_order(**kwargs):
        calls["count"] += 1
        return _Resp()

    strategy._place_order = _fake_place_order  # type: ignore[assignment]

    strategy._enter_position(DummyCandle(101.0), vwap=100.0)
    assert calls["count"] == 1
    assert strategy.state.position is not None
