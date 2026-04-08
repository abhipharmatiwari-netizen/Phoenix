import logging

from app.strategies.ema20_strategy import Ema20Strategy


def _make_strategy():
    return Ema20Strategy(
        instrument_meta={},
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
    )


def test_ema20_qty_blank_env_defaults(monkeypatch, caplog):
    monkeypatch.setenv("NIFTY_EMA20_LOTS", "")
    monkeypatch.setenv("EMA20_LOTS", "")
    monkeypatch.setenv("NIFTY_EMA20_QTY", "")
    monkeypatch.setenv("EMA20_QTY", "")
    with caplog.at_level(logging.WARNING):
        strat = _make_strategy()
        qty = strat._resolve_qty({})
        qty_again = strat._resolve_qty({})
    assert qty == 1
    assert qty_again == 1
    default_logs = [rec for rec in caplog.records if "EMA20 qty defaulted" in rec.message]
    assert len(default_logs) == 1
    assert any(
        "keys=" in rec.message and "values=" in rec.message
        for rec in caplog.records
    )


def test_ema20_qty_prefers_underlying_lots_key(monkeypatch):
    monkeypatch.setenv("NIFTY_EMA20_LOTS", "2")
    monkeypatch.setenv("EMA20_LOTS", "5")
    monkeypatch.setenv("NIFTY_EMA20_QTY", "7")
    monkeypatch.setenv("EMA20_QTY", "9")
    strat = _make_strategy()
    assert strat._resolve_qty({}) == 2


def test_ema20_qty_falls_back_to_legacy_qty_when_lots_unset(monkeypatch):
    monkeypatch.delenv("NIFTY_EMA20_LOTS", raising=False)
    monkeypatch.delenv("EMA20_LOTS", raising=False)
    monkeypatch.setenv("NIFTY_EMA20_QTY", "2")
    monkeypatch.setenv("EMA20_QTY", "5")
    strat = _make_strategy()
    assert strat._resolve_qty({}) == 2


def test_ema20_qty_startup_summary_shows_resolved_values(monkeypatch, caplog):
    monkeypatch.setenv("NIFTY_EMA20_LOTS", "2")
    monkeypatch.setenv("EMA20_LOTS", "5")
    monkeypatch.setenv("NIFTY_EMA20_QTY", "7")
    monkeypatch.setenv("EMA20_QTY", "9")

    with caplog.at_level(logging.INFO, logger="app.strategies.ema20_strategy"):
        strat = _make_strategy()

    assert strat._resolve_qty({}) == 2
    assert "EMA20 lots resolved" in caplog.text
    assert "lots=2" in caplog.text
    assert "key=NIFTY_EMA20_LOTS" in caplog.text
