from pathlib import Path


HTML_PATH = Path(__file__).resolve().parents[2] / "app" / "static" / "dashboard.html"


def _dashboard_html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def test_recent_trades_fix_is_flag_gated():
    html = _dashboard_html()
    assert 'const DASHBOARD_FIX_RECENT_TRADES = "__DASHBOARD_FIX_RECENT_TRADES__" === "true";' in html
    assert "function renderTradesLegacy(trades)" in html
    assert "if (!DASHBOARD_FIX_RECENT_TRADES)" in html


def test_recent_trades_fix_uses_trade_payload_and_appends_rows():
    html = _dashboard_html()
    assert "const instrument = trade.instrument || trade.symbol || trade.label || trade.underlying || \"--\";" in html
    assert "row.innerHTML = `" in html
    assert "tradesListEl.appendChild(row);" in html


def test_recent_trades_fix_keeps_empty_state():
    html = _dashboard_html()
    assert "No trades yet." in html
    assert "const items = Array.isArray(trades) ? trades : [];" in html
