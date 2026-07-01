from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
NGINX_TEMPLATE = REPO_ROOT / "nginx" / "nginx.conf.template"
NGINX_SSL_TEMPLATE = REPO_ROOT / "nginx" / "nginx-ssl.conf.template"
NGINX_DOCKERFILE = REPO_ROOT / "nginx" / "Dockerfile"


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

    assert "location = /nginx-health" in content
    assert 'return 200 "ok\\n";' in content
    assert "proxy_pass http://backend/readyz-public;" in content
    assert "proxy_pass http://backend/health/summary-public;" in content
    assert "location = /health/alerts" in content
    assert "location = /health/mitigations" in content


def test_public_health_routes_use_internal_backend_host_header():
    for template in (NGINX_TEMPLATE, NGINX_SSL_TEMPLATE):
        content = template.read_text()

        for path in ("/health", "/health/summary", "/readyz"):
            pattern = rf"location\s+=\s+{re.escape(path)}\s+\{{.*?proxy_set_header Host backend;"
            assert re.search(pattern, content, flags=re.S), f"{template.name} {path}"


def test_nginx_runtime_contains_healthcheck_binary():
    dockerfile = NGINX_DOCKERFILE.read_text()

    assert "apt-get install -y --no-install-recommends wget" in dockerfile


def test_overview_health_cards_fit_without_horizontal_scroll():
    app_css = (REPO_ROOT / "frontend" / "src" / "App.css").read_text(encoding="utf-8")

    assert ".main-content" in app_css
    assert ".content" in app_css
    assert ".health-tiles" in app_css
    assert "min-width: 0;" in app_css

    health_tiles_block = re.search(r"\.health-tiles\s+\{(?P<body>.*?)\}", app_css, flags=re.S)
    assert health_tiles_block is not None
    body = health_tiles_block.group("body")
    assert "display: grid;" in body
    assert "grid-template-columns: repeat(6, minmax(0, 1fr));" in body
    assert "display: flex;" not in body


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


def test_sensitive_probe_paths_do_not_fall_back_to_spa_html():
    for template in (NGINX_TEMPLATE, NGINX_SSL_TEMPLATE):
        content = template.read_text()

        assert "location ~ /\\.(?!well-known/acme-challenge/)" in content
        assert "location ~* ^/(?:___proxy_subdomain_whm|cgi-bin|phpmyadmin" in content
        assert "location ~* ^/(?:wp-config(?:\\.|/|$)|login\\.html$)" in content
        assert "return 404;" in content

        first_sensitive_location = content.index(
            "location ~* ^/(?:___proxy_subdomain_whm|cgi-bin|phpmyadmin"
        )
        spa_fallback = content.rindex("try_files $uri $uri/ /index.html;")
        assert first_sensitive_location < spa_fallback


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
    client = (REPO_ROOT / "frontend" / "src" / "client" / "index.ts").read_text(encoding="utf-8")

    assert "String(status || 'unknown').toLowerCase()" in overview
    assert "healthSummary?.alerts?.firing_count ?? 0" in overview
    assert "healthSummary?.degraded_reasons || []" in overview
    assert "healthSummary?.schema_status || healthSummary?.schema?.status || 'unknown'" in overview
    assert "publicHealth?.ready" in overview
    assert "bffPath('/admin/health/summary')" in client
    assert "path: '/health/summary'" in client


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
