import importlib
import types
import pytest
from pathlib import Path

MODULE_PATH = "app.core.strategy_config_loader"
CALLABLE_NAMES = ["_set_env_if_missing", "load_strategy_env_from_yaml", "load_universe_from_yaml"]


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


def test_loader_exports_risk_auto_resize_toggle(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    yaml_path = Path(__file__).resolve().parent / "_strategy_env_toggle_test.yaml"
    monkeypatch.delenv("RISK_CAPITAL_AUTO_RESIZE_ENABLED", raising=False)
    try:
        yaml_path.write_text(
            "risk:\n"
            "  capital_auto_resize_enabled: false\n",
            encoding="utf-8",
        )
        mod.load_strategy_env_from_yaml(yaml_path)
        assert (
            str(mod.os.getenv("RISK_CAPITAL_AUTO_RESIZE_ENABLED", "")).lower()
            == "false"
        )
    finally:
        try:
            yaml_path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            mod.os.environ.pop("RISK_CAPITAL_AUTO_RESIZE_ENABLED", None)
        except Exception:
            pass


def test_loader_canonicalizes_strategy_and_allowed_names(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    yaml_path = Path(__file__).resolve().parent / "_strategy_env_names_test.yaml"
    try:
        yaml_path.write_text(
            "strategies:\n"
            "  - name: options_generic\n"
            "    enabled: true\n"
            "    instruments: [\"NG_FUT\"]\n"
            "instruments:\n"
            "  NG_FUT:\n"
            "    enabled: true\n"
            "    allowed_strategies: [\"ema20\", \"put-buy\"]\n",
            encoding="utf-8",
        )
        cfg = mod.load_strategy_env_from_yaml(yaml_path)
        assert cfg["strategies"][0]["name"] == "option_strategy"
        assert cfg["instruments"]["NG_FUT"]["allowed_strategies"] == [
            "ema20_strategy",
            "put_buy",
        ]
    finally:
        try:
            yaml_path.unlink(missing_ok=True)
        except Exception:
            pass


def test_loader_accepts_strategy_instrument_override_rows(tmp_path):
    mod = importlib.import_module(MODULE_PATH)
    yaml_path = tmp_path / "strategy_env_instrument_override.yaml"
    yaml_path.write_text(
        "strategies:\n"
        "  - name: put_momentum_scalper\n"
        "    enabled: true\n"
        "    instruments:\n"
        "      - label: NIFTY_IDX\n"
        "        entry_start: '09:20'\n"
        "        entry_end: '14:45'\n"
        "      - BANKNIFTY_IDX\n",
        encoding="utf-8",
    )

    cfg = mod.load_strategy_env_from_yaml(yaml_path)
    assert cfg["strategies"][0]["name"] == "put_momentum_scalper"
    assert cfg["strategies"][0]["instruments"][0]["label"] == "NIFTY_IDX"
    assert cfg["strategies"][0]["instruments"][0]["entry_end"] == "14:45"
    assert cfg["strategies"][0]["instruments"][1] == "BANKNIFTY_IDX"


def test_loader_accepts_oi_ml_ce_seller_name(tmp_path):
    mod = importlib.import_module(MODULE_PATH)
    yaml_path = tmp_path / "strategy_env_oi_ml_ce_seller.yaml"
    yaml_path.write_text(
        "strategies:\n"
        "  - name: oi_ml_ce_seller\n"
        "    enabled: false\n"
        "    instruments: [\"NIFTY_IDX\"]\n"
        "instruments:\n"
        "  NIFTY_IDX:\n"
        "    enabled: true\n"
        "    allowed_strategies: [\"oi_ml_ce_seller\"]\n",
        encoding="utf-8",
    )

    cfg = mod.load_strategy_env_from_yaml(yaml_path)

    assert cfg["strategies"][0]["name"] == "oi_ml_ce_seller"
    assert cfg["instruments"]["NIFTY_IDX"]["allowed_strategies"] == [
        "oi_ml_ce_seller"
    ]


def test_loader_accepts_directional_regime_keys_in_selector_and_profiles(tmp_path):
    mod = importlib.import_module(MODULE_PATH)
    yaml_path = tmp_path / "strategy_env_directional_regime.yaml"
    yaml_path.write_text(
        "strategies:\n"
        "  - name: ema20_strategy\n"
        "    enabled: true\n"
        "    instruments: [\"NIFTY_IDX\"]\n"
        "    params:\n"
        "      dynamic_policy:\n"
        "        enabled: true\n"
        "        policy_id: test_policy\n"
        "        profiles:\n"
        "          TRENDING_UP:\n"
        "            qty_mult: 1.1\n"
        "          TRENDING_DOWN:\n"
        "            qty_mult: 0.8\n"
        "strategy_selection:\n"
        "  enabled: true\n"
        "  mapping:\n"
        "    NIFTY_IDX:\n"
        "      TRENDING_UP: [\"exclusive_nifty_ce_buy\"]\n"
        "      TRENDING_DOWN: [\"put_momentum_scalper\"]\n",
        encoding="utf-8",
    )

    cfg = mod.load_strategy_env_from_yaml(yaml_path)

    profiles = cfg["strategies"][0]["params"]["dynamic_policy"]["profiles"]
    assert "TRENDING_UP" in profiles
    assert "TRENDING_DOWN" in profiles
    assert cfg["strategy_selection"]["mapping"]["NIFTY_IDX"]["TRENDING_UP"] == [
        "exclusive_nifty_ce_buy"
    ]


def test_loader_rejects_invalid_strategy_schema(tmp_path):
    mod = importlib.import_module(MODULE_PATH)
    yaml_path = tmp_path / "invalid_strategy_env.yaml"
    yaml_path.write_text(
        "strategies:\n"
        "  - enabled: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Schema validation failed"):
        mod.load_strategy_env_from_yaml(yaml_path)


def test_loader_rejects_invalid_universe_schema(tmp_path):
    mod = importlib.import_module(MODULE_PATH)
    yaml_path = tmp_path / "invalid_universe.yaml"
    yaml_path.write_text(
        "underlyings:\n"
        "  - name: NIFTY\n"
        "    type: INDEX\n"
        "    segment: NFO\n"
        "    lot_size: 0\n"
        "    enabled: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Schema validation failed"):
        mod.load_universe_from_yaml(yaml_path)
