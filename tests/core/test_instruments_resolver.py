import importlib
import types
from datetime import datetime, timedelta

import pandas as pd
import pytest

MODULE_PATH = "app.core.instruments_resolver"
CALLABLE_NAMES = ['load_scrip_master', 'get_front_future_for_symbol', 'get_front_future', 'get_atm_options', 'find_index_underlying', 'pick_index_options_strikes']


def test_import_module_succeeds():
    mod = importlib.import_module(MODULE_PATH)
    assert isinstance(mod, types.ModuleType)


@pytest.mark.parametrize("name", CALLABLE_NAMES)
def test_module_defines_callable(name):
    mod = importlib.import_module(MODULE_PATH)
    assert hasattr(mod, name), f"Module {MODULE_PATH} should define {name}"


@pytest.mark.parametrize("name", CALLABLE_NAMES)
def test_callable_behaviour_placeholder(name):
    mod = importlib.import_module(MODULE_PATH)
    obj = getattr(mod, name)
    assert obj is not None


def test_get_front_future_and_option_picker_include_normalized_tick_size():
    mod = importlib.import_module(MODULE_PATH)
    expiry_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=30)
    expiry_text = expiry_dt.strftime("%d%b%Y").upper()
    symbol_expiry = expiry_dt.strftime("%d%b%y").upper()
    df = pd.DataFrame(
        [
            {
                "symbol": f"NATURALGAS{symbol_expiry}FUT",
                "expiry": expiry_text,
                "exch_seg": "MCX",
                "token": "505100",
                "tick_size": 5.0,
            },
            {
                "symbol": f"NATURALGAS{symbol_expiry}310CE",
                "expiry": expiry_text,
                "exch_seg": "MCX",
                "token": "505131",
                "strike": 31000,
                "lotsize": 1250,
                "tick_size": 5.0,
            },
            {
                "symbol": f"NATURALGAS{symbol_expiry}310PE",
                "expiry": expiry_text,
                "exch_seg": "MCX",
                "token": "505132",
                "strike": 31000,
                "lotsize": 1250,
                "tick_size": 5.0,
            },
        ]
    )

    future = mod.get_front_future_for_symbol(df, "NATURALGAS", segments=["MCX"])
    picks = mod.pick_index_options_strikes(
        df,
        base_symbol="NATURALGAS",
        index_ltp=309.3,
        segments=["MCX"],
        expiry_dt=expiry_dt,
    )

    assert future["tick_size"] == pytest.approx(0.05)
    assert picks["atm_ce"]["tick_size"] == pytest.approx(0.05)
    assert picks["atm_pe"]["tick_size"] == pytest.approx(0.05)
