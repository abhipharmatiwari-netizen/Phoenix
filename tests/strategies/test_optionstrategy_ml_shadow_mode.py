from datetime import datetime, timezone

from app.config.boot_config import StrategyValueResolver
from app.strategies.option_strategy import OptionStrategy


class DummyPersister:
    def __init__(self) -> None:
        self.records = []

    def persist_decision(self, record):
        self.records.append(record)


class DummyRiskManager:
    def __init__(self, persister) -> None:
        self.trade_persister = persister


class DummyModelStore:
    def predict(self, underlying, features, now_ts=None):
        raise AssertionError("OptionStrategy should not invoke ML model_store")


class DummyFeatureAssembler:
    def assemble(
        self,
        underlying,
        candle,
        indicators,
        recent_closes=None,
        now_ts=None,
        risk_ctx=None,
    ):
        raise AssertionError("OptionStrategy should not invoke ML feature_assembler")


class DummyCandle:
    def __init__(self, close: float) -> None:
        self.c = close
        self.close = close
        self.start_ts = datetime.now(timezone.utc)
        self.end_ts = self.start_ts


def test_optionstrategy_shadow_components_are_ignored() -> None:
    persister = DummyPersister()
    risk_manager = DummyRiskManager(persister)
    model_store = DummyModelStore()
    assembler = DummyFeatureAssembler()
    instrument_meta = {"NG_FUT": {"kind": "UNDERLYING", "lot_size": 1}}

    strat = OptionStrategy(
        instrument_meta=instrument_meta,
        order_client=None,
        env_prefix="NG_",
        underlying_label="NG_FUT",
        ce_label_prefix="NG_ATM_CE",
        pe_label_prefix="NG_ATM_PE",
        risk_manager=risk_manager,
        model_store=model_store,
        feature_assembler=assembler,
        config_resolver=StrategyValueResolver(
            env_prefix="NG_",
            params={},
            env={},
        ),
    )
    strat.prev_rsi_prev = 52.0
    strat.prev_rsi = 51.0
    strat.prev_macd = 0.1
    strat.prev_ema = 100.0
    strat.prev_close = 100.0
    strat.min_atr = 0.1

    fired = {"called": False}

    def _fake_fire(template_name, reason=None, ml_info=None):
        fired["called"] = True
        fired["ml_info"] = ml_info

    strat._fire_template = _fake_fire  # type: ignore[assignment]

    candle = DummyCandle(101.0)
    indicators = {
        "atr": 0.4,
        "rsi": 50.0,
        "macd": 0.1,
        "macd_signal": 0.05,
        "macd_hist": 0.01,
        "ema_20": 100.0,
    }

    strat.on_bar(label="NG_FUT", timeframe_seconds=strat.signal_timeframe, candle=candle, indicators=indicators)

    assert fired["called"] is True
    assert fired["ml_info"] is None
    assert persister.records == []
