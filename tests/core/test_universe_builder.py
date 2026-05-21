import importlib
import types
from datetime import datetime

import pytest

MODULE_PATH = "app.core.universe_builder"
CALLABLE_NAMES = ['_exchange_type_for_segment', '_format_strike', '_override_token', '_build_secure_headers', 'get_ltp_for_token', 'get_ltp_for_tokens', '_load_universe', '_build_future_underlying', '_build_index_underlying', 'build_instrument_universe']


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


def test_full_instrument_list_logging_defaults_off_in_live(monkeypatch):
    monkeypatch.delenv("UNIVERSE_LOG_FULL_LIST", raising=False)
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    mod = importlib.import_module(MODULE_PATH)

    assert mod._should_log_full_instrument_list() is False


def test_full_instrument_list_logging_can_be_forced_in_live(monkeypatch):
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.setenv("UNIVERSE_LOG_FULL_LIST", "1")
    mod = importlib.import_module(MODULE_PATH)

    assert mod._should_log_full_instrument_list() is True


def _fake_options(expiry):
    def opt(token, symbol, strike, exch="NFO"):
        return {
            "token": token,
            "symbol": symbol,
            "strike": float(strike),
            "exch_seg": exch,
            "lot_size": 75,
            "tick_size": 0.05,
        }

    return {
        "expiry_dt": expiry,
        "atm_ce": opt("101", "NIFTY26MAY2623600CE", 23600),
        "atm_pe": opt("102", "NIFTY26MAY2623600PE", 23600),
        "otm_ce": opt("103", "NIFTY26MAY2623650CE", 23650),
        "otm_pe": opt("104", "NIFTY26MAY2623550PE", 23550),
        "otm_pairs": [
            {
                "ce": opt("103", "NIFTY26MAY2623650CE", 23650),
                "pe": opt("104", "NIFTY26MAY2623550PE", 23550),
            }
        ],
        "itm_ce": opt("105", "NIFTY26MAY2623550CE", 23550),
        "itm_pe": opt("106", "NIFTY26MAY2623650PE", 23650),
    }


def test_index_option_metadata_carries_resolved_expiry(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    expiry = datetime(2026, 5, 26)
    monkeypatch.setattr(mod, "get_ltp_for_token", lambda *_args, **_kwargs: 23600.0)
    monkeypatch.setattr(
        mod,
        "pick_index_options_strikes",
        lambda *_args, **_kwargs: _fake_options(expiry),
    )

    instruments = mod._build_index_underlying(
        {
            "name": "NIFTY",
            "lot_size": 75,
            "options": {
                "use_atm": True,
                "use_strangle_otm": True,
                "use_itm": True,
            },
        },
        df=object(),
        jwt_token="jwt",
        api_key="api",
    )

    option_metas = [inst for inst in instruments if inst["kind"] != "UNDERLYING"]
    assert option_metas
    assert all(meta["expiry"] == "2026-05-26" for meta in option_metas)
    assert all(meta["expiry_dt"] == "2026-05-26" for meta in option_metas)


def test_future_option_metadata_carries_resolved_expiry(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    expiry = datetime(2026, 5, 26)
    monkeypatch.setattr(
        mod,
        "get_front_future",
        lambda _df: {
            "token": "201",
            "symbol": "NATURALGAS26MAY26FUT",
            "exch_seg": "MCX",
            "expiry_dt": expiry,
            "tick_size": 0.05,
        },
    )
    monkeypatch.setattr(mod, "get_ltp_for_token", lambda *_args, **_kwargs: 300.0)
    monkeypatch.setattr(
        mod,
        "pick_index_options_strikes",
        lambda *_args, **_kwargs: _fake_options(expiry),
    )

    instruments = mod._build_future_underlying(
        {
            "name": "NG",
            "symbol_pattern": "NATURALGAS",
            "lot_size": 1250,
            "options": {
                "use_atm": True,
                "use_strangle_otm": True,
            },
        },
        df=object(),
        jwt_token="jwt",
        api_key="api",
    )

    option_metas = [inst for inst in instruments if inst["kind"] != "UNDERLYING"]
    assert option_metas
    assert all(meta["expiry"] == "2026-05-26" for meta in option_metas)
    assert all(meta["expiry_dt"] == "2026-05-26" for meta in option_metas)
