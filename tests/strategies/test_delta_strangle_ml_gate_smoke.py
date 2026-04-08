from datetime import datetime, timezone

from app.strategies.delta_strangle import DeltaStrangleStrategy


class DummyMLGate:
    def evaluate_candidate(self, **kwargs):
        raise AssertionError("DeltaStrangle should not invoke ml_gate")


class DummyRiskManager:
    def __init__(self):
        self.trade_persister = None
        self.current_trade_mode = "PAPER"

    def place_order(self, **kwargs):
        class Decision:
            allowed = False
            reason = "blocked"

        return Decision()


def test_delta_strangle_ignores_ml_gate_and_places_orders():
    strat = DeltaStrangleStrategy(
        instrument_meta={
            "NIFTY_OTM_CE_1": {"underlying": "NIFTY", "kind": "OTM_CE", "strike": 1, "lot_size": 65},
            "NIFTY_OTM_PE_1": {"underlying": "NIFTY", "kind": "OTM_PE", "strike": 1, "lot_size": 65},
        },
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        risk_manager=DummyRiskManager(),
        delta_qty_per_leg=1,
        entry_start_ist="00:00",
        entry_end_ist="23:59",
        ml_gate=DummyMLGate(),
    )
    strat.last_price["NIFTY_OTM_CE_1"] = 10.0
    strat.last_price["NIFTY_OTM_PE_1"] = 9.0
    strat.last_price["NIFTY_IDX"] = 100.0

    fired = {"orders": 0}

    def fake_place(*args, **kwargs):
        fired["orders"] += 1
        return True

    strat._place_order = fake_place  # type: ignore
    strat._maybe_fire_strangle()
    assert fired["orders"] == 2
