import importlib
import inspect
from pathlib import Path


STRATEGY_CLASSES = [
    ("app.strategies.ce_orb", "CeOrbStrategy"),
    ("app.strategies.ce_pdh_rb", "CePdhRangeBreakoutStrategy"),
    ("app.strategies.delta_strangle", "DeltaStrangleStrategy"),
    ("app.strategies.ema20_strategy", "Ema20Strategy"),
    ("app.strategies.exclusive_nifty_ce_buy", "ExclusiveNiftyCeBuyStrategy"),
    ("app.strategies.nifty_weekly_credit_spreads", "NiftyWeeklyCreditSpreadStrategy"),
    ("app.strategies.option_strategy", "OptionStrategy"),
    ("app.strategies.put_buy_live", "PutBuyLiveStrategy"),
    ("app.strategies.put_momentum_scalper", "PutMomentumScalperStrategy"),
    ("app.strategies.put_reversion_writer", "PutReversionWriterStrategy"),
]

STRATEGY_FILES = [
    "app/strategies/ce_orb.py",
    "app/strategies/ce_pdh_rb.py",
    "app/strategies/delta_strangle.py",
    "app/strategies/ema20_strategy.py",
    "app/strategies/exclusive_nifty_ce_buy.py",
    "app/strategies/nifty_weekly_credit_spreads.py",
    "app/strategies/option_strategy.py",
    "app/strategies/put_buy_live.py",
    "app/strategies/put_momentum_scalper.py",
    "app/strategies/put_reversion_writer.py",
]


def test_strategy_classes_conform_to_contract_methods():
    for module_path, class_name in STRATEGY_CLASSES:
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        assert hasattr(cls, "on_tick"), f"{class_name} missing on_tick"
        assert hasattr(cls, "on_bar"), f"{class_name} missing on_bar"
        assert hasattr(cls, "force_exit_all"), f"{class_name} missing force_exit_all"
        assert callable(getattr(cls, "on_tick"))
        assert callable(getattr(cls, "on_bar"))
        assert callable(getattr(cls, "force_exit_all"))


def test_strategy_contract_signatures_expose_required_hooks():
    for module_path, class_name in STRATEGY_CLASSES:
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)

        on_tick_sig = inspect.signature(getattr(cls, "on_tick"))
        on_bar_sig = inspect.signature(getattr(cls, "on_bar"))
        force_exit_sig = inspect.signature(getattr(cls, "force_exit_all"))

        assert "label" in on_tick_sig.parameters
        assert "ltp" in on_tick_sig.parameters
        assert "label" in on_bar_sig.parameters
        assert "timeframe_seconds" in on_bar_sig.parameters
        assert "candle" in on_bar_sig.parameters
        assert "indicators" in on_bar_sig.parameters
        assert "reason" in force_exit_sig.parameters


def test_strategy_modules_do_not_contain_tenant_routing_resolution_logic():
    disallowed_tokens = [
        "resolve_hub_route_ids",
        "_tenant_id",
        "_broker_account_id",
        "get_global_routing_table",
    ]
    for rel_path in STRATEGY_FILES:
        text = Path(rel_path).read_text(encoding="utf-8")
        for token in disallowed_tokens:
            assert token not in text, f"{rel_path} still contains {token}"
