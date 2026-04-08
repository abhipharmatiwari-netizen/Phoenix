from __future__ import annotations

import importlib
import importlib.util


def test_entrypoints_import_without_ml_runtime_dependency() -> None:
    assert importlib.import_module("app.main") is not None
    assert importlib.import_module("app.server") is not None
    assert importlib.import_module("app.config.boot_config") is not None
    assert importlib.util.find_spec("app.ml") is None


def test_ema20_strategy_init_does_not_touch_model_artifacts(monkeypatch) -> None:
    from pathlib import Path
    from app.strategies.ema20_strategy import Ema20Strategy

    _orig_exists = Path.exists

    def _guard_exists(self: Path) -> bool:  # pragma: no cover - defensive guard
        normalized = str(self).replace("\\", "/").lower()
        if "app/models/lgbm" in normalized:
            raise AssertionError("EMA20 init should not touch app/models/lgbm")
        return _orig_exists(self)

    monkeypatch.setattr(Path, "exists", _guard_exists)

    instrument_meta = {
        "NG_FUT": {"symbol": "NG_FUT"},
        "NG_ATM_CE_100": {
            "symbol": "NATURALGAS20FEB26100CE",
            "token": "123",
            "lot_size": 1,
            "exchange": "MCX",
        },
    }
    strategy = Ema20Strategy(
        instrument_meta,
        env_prefix="NG_",
        underlying_label="NG_FUT",
        signal_timeframe=300,
        ema_period=20,
        require_rsi_falling=False,
    )
    assert strategy is not None
    assert strategy._allow_trade_by_ml(candle=object(), indicators={}) is True
