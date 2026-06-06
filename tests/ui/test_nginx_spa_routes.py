from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
NGINX_TEMPLATE = REPO_ROOT / "nginx" / "nginx.conf.template"


def test_positions_route_is_not_proxied_over_spa():
    content = NGINX_TEMPLATE.read_text()

    assert "location /positions" not in content
    assert "try_files $uri $uri/ /index.html;" in content


def test_index_html_is_marked_non_cacheable_for_route_refreshes():
    content = NGINX_TEMPLATE.read_text()

    assert 'location = /index.html' in content
    assert 'Cache-Control "no-store"' in content


def test_public_health_routes_use_redacted_backend_endpoints():
    content = NGINX_TEMPLATE.read_text()

    assert "proxy_pass http://backend/readyz-public;" in content
    assert "proxy_pass http://backend/health/summary-public;" in content
    assert "location = /health/alerts" in content
    assert "location = /health/mitigations" in content


def test_frontend_assets_do_not_fall_back_to_spa_html():
    content = NGINX_TEMPLATE.read_text()

    assert "location /static/" in content
    assert "try_files $uri =404;" in content
    assert "location = /manifest.json" in content
    assert "try_files /manifest.json =404;" in content
    assert "location = /favicon.svg" in content
    assert "try_files /favicon.svg =404;" in content
    assert "location = /favicon.ico" in content
    assert "rewrite ^ /favicon.svg last;" in content


def test_frontend_public_index_references_existing_static_assets():
    public_dir = REPO_ROOT / "frontend" / "public"
    index = (public_dir / "index.html").read_text(encoding="utf-8")

    refs = re.findall(r'href="%PUBLIC_URL%/([^"]+)"', index)

    assert "favicon.ico" not in refs
    assert "logo192.png" not in refs
    assert refs
    for ref in refs:
        assert (public_dir / ref).is_file(), ref

    assert "Loading Phoenix..." in index


def test_frontend_runtime_failures_do_not_blank_root():
    client = (REPO_ROOT / "frontend" / "src" / "client" / "index.ts").read_text(encoding="utf-8")
    entrypoint = (REPO_ROOT / "frontend" / "src" / "index.tsx").read_text(encoding="utf-8")
    boundary = (
        REPO_ROOT / "frontend" / "src" / "components" / "shared" / "AppErrorBoundary.tsx"
    ).read_text(encoding="utf-8")

    assert "safeLocalStorageGetItem" in client
    assert "safeLocalStorageSetItem" in client
    assert "safeLocalStorageRemoveItem" in client
    assert "<AppErrorBoundary>" in entrypoint
    assert "Phoenix could not render" in boundary
    assert "Reset session" in boundary


def test_overview_tolerates_public_redacted_health_summary():
    overview = (REPO_ROOT / "frontend" / "src" / "pages" / "Overview.tsx").read_text(encoding="utf-8")

    assert "String(status || 'unknown').toLowerCase()" in overview
    assert "healthSummary?.alerts?.firing_count ?? 0" in overview
    assert "healthSummary?.degraded_reasons || []" in overview
    assert "healthSummary?.schema_status || healthSummary?.schema?.status || 'unknown'" in overview
    assert "publicHealth?.ready" in overview


def test_alerts_and_mitigations_tolerate_missing_response_arrays():
    alerts = (REPO_ROOT / "frontend" / "src" / "pages" / "Alerts.tsx").read_text(encoding="utf-8")
    mitigations = (REPO_ROOT / "frontend" / "src" / "pages" / "Mitigations.tsx").read_text(encoding="utf-8")

    assert "Array.isArray(response?.alerts) ? response.alerts : []" in alerts
    assert "Array.isArray(response?.recent_events) ? response.recent_events : []" in mitigations
    assert "fault_counts: response?.fault_counts && typeof response.fault_counts === 'object'" in mitigations


def test_safety_treats_omitted_public_watchdog_as_unknown():
    safety = (REPO_ROOT / "frontend" / "src" / "pages" / "Safety.tsx").read_text(encoding="utf-8")

    assert "runtimeStatus(health?.watchdog_running)" in safety
    assert "return { status: 'warning' as const, label: 'Unknown' };" in safety
    assert "trackedAccountCount ?? 'Unknown'" in safety
