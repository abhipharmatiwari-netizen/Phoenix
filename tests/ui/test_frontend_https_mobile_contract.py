from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"


def read_frontend_source() -> str:
    parts: list[str] = []
    for path in FRONTEND_SRC.rglob("*"):
        if path.suffix in {".ts", ".tsx", ".css"}:
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_frontend_viewport_is_safe_area_aware():
    index = (REPO_ROOT / "frontend" / "public" / "index.html").read_text(encoding="utf-8")

    assert 'name="viewport"' in index
    assert "width=device-width, initial-scale=1, viewport-fit=cover" in index


def test_frontend_runtime_does_not_hardcode_insecure_remote_origins():
    source = read_frontend_source()

    insecure_urls = sorted(set(re.findall(r"http://(?!localhost(?::|/)|127\.0\.0\.1(?::|/))[^'\"\\s)]+", source)))
    assert insecure_urls == []
    assert "http://65.20.69.50" not in source


def test_client_url_helpers_prefer_same_origin_and_https_websockets():
    client = (FRONTEND_SRC / "client" / "index.ts").read_text(encoding="utf-8")

    assert "sanitizeConfiguredHttpBaseUrl" in client
    assert "parsed.protocol === 'http:'" in client
    assert "isHttpsPage()" in client
    assert "return '';" in client
    assert "return queryString ? `${relativePath}?${queryString}` : relativePath;" in client
    assert "httpBaseUrlToWebSocketBaseUrl" in client
    assert "window.location.protocol === 'https:' ? 'wss:' : 'ws:'" in client
    assert "parsed.protocol === 'ws:'" in client


def test_mobile_shell_uses_drawer_safe_areas_and_bounded_overflow():
    app_css = (FRONTEND_SRC / "App.css").read_text(encoding="utf-8")
    side_nav_css = (FRONTEND_SRC / "components" / "layout" / "SideNav.css").read_text(encoding="utf-8")
    top_nav_css = (FRONTEND_SRC / "components" / "layout" / "TopNav.css").read_text(encoding="utf-8")
    shell = (FRONTEND_SRC / "components" / "layout" / "Shell.tsx").read_text(encoding="utf-8")
    top_nav = (FRONTEND_SRC / "components" / "layout" / "TopNav.tsx").read_text(encoding="utf-8")
    side_nav = (FRONTEND_SRC / "components" / "layout" / "SideNav.tsx").read_text(encoding="utf-8")

    assert "height: 100dvh;" in app_css
    assert "overflow-x: hidden;" in app_css
    assert "env(safe-area-inset-bottom)" in app_css
    assert "@media (max-height: 430px) and (orientation: landscape)" in app_css
    assert ".side-nav-overlay.is-open" in app_css

    assert "transform: translateX(-100%);" in side_nav_css
    assert ".side-nav.is-open" in side_nav_css
    assert "height: 100dvh;" in side_nav_css

    assert ".mobile-menu-button" in top_nav_css
    assert "aria-expanded={isMobileNavOpen}" in top_nav
    assert 'aria-controls="primary-navigation"' in top_nav
    assert 'id="primary-navigation"' in side_nav
    assert 'aria-label="Primary navigation"' in side_nav
    assert "setMobileNavOpen(false)" in shell


def test_tables_and_preformatted_blocks_are_internally_scrollable():
    app_css = (FRONTEND_SRC / "App.css").read_text(encoding="utf-8")
    data_table = (FRONTEND_SRC / "components" / "shared" / "DataTable.tsx").read_text(encoding="utf-8")
    alerts = (FRONTEND_SRC / "pages" / "Alerts.tsx").read_text(encoding="utf-8")
    mitigations = (FRONTEND_SRC / "pages" / "Mitigations.tsx").read_text(encoding="utf-8")

    assert ".table-scroll" in app_css
    assert "overflow-x: auto;" in app_css
    assert "min-width: max-content;" in app_css
    assert ".json-block" in app_css
    assert "overflow-wrap: anywhere;" in app_css
    assert ".system-degraded-panel" in app_css
    assert "maxWidth: '100%'" in data_table
    assert "WebkitOverflowScrolling: 'touch'" in data_table
    assert "<div className=\"table-scroll\">" in alerts
    assert "<div className=\"table-scroll\">" in mitigations
    assert 'className="json-block"' in mitigations
