from pathlib import Path
import re
import pytest


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
    for template in (NGINX_TEMPLATE, NGINX_SSL_TEMPLATE):
        content = template.read_text()

        assert "location = /nginx-health" in content
        assert 'return 200 "ok\\n";' in content
        assert "proxy_pass http://backend/readyz-public;" in content
        assert "proxy_pass http://backend/health/summary-public;" in content
        assert "location = /health/alerts" in content
        assert "location = /health/mitigations" in content
        assert "Content-Security-Policy" in content
        assert "Strict-Transport-Security" in content


def test_public_health_routes_use_internal_backend_host_header():
    for template in (NGINX_TEMPLATE, NGINX_SSL_TEMPLATE):
        content = template.read_text()

        for path in ("/health", "/health/summary", "/readyz"):
            pattern = rf"location\s+=\s+{re.escape(path)}\s+\{{.*?proxy_set_header Host backend;"
            assert re.search(pattern, content, flags=re.S), f"{template.name} {path}"


def test_nginx_runtime_contains_healthcheck_binary():
    dockerfile = NGINX_DOCKERFILE.read_text()

    assert "apt-get install -y --no-install-recommends wget" in dockerfile


def test_frontend_assets_do_not_fall_back_to_spa_html():
    for template in (NGINX_TEMPLATE, NGINX_SSL_TEMPLATE):
        content = template.read_text()

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

    assert "classifyOperatorHealth" in overview
    assert "public summary proves reachability only" in overview
    assert "Authenticated admin diagnostics unavailable" in overview
    assert "bffPath('/admin/health/summary')" in client
    assert "path: '/health/summary'" in client
    assert "source: 'public'" in client


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
    assert "classifyOperatorHealth" in safety
    assert "healthSource !== 'admin'" in safety


def test_safety_merges_break_glass_flatten_audit_feed():
    safety = (REPO_ROOT / "frontend" / "src" / "pages" / "Safety.tsx").read_text(encoding="utf-8")

    assert "resource_type: 'position'" in safety
    assert "action: 'break_glass_flatten'" in safety
    assert "action: 'break_glass', limit: 20" not in safety


def test_admin_console_routes_are_wired():
    app = (REPO_ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    nav = (REPO_ROOT / "frontend" / "src" / "components" / "layout" / "SideNav.tsx").read_text(encoding="utf-8")

    for route in (
        "/strategies",
        "/accounts",
        "/audit",
        "/release-evidence",
        "/settings",
    ):
        assert f'path="{route}"' in app
        assert f'to: \'{route}\'' in nav or f'to="{route}"' in nav


def test_nginx_security_headers_are_hardened():
    for template in (NGINX_TEMPLATE, NGINX_SSL_TEMPLATE):
        content = template.read_text()
        assert "Content-Security-Policy" in content
        assert "frame-ancestors 'self'" in content
        assert "X-Content-Type-Options \"nosniff\"" in content
        assert "Referrer-Policy \"strict-origin-when-cross-origin\"" in content
        assert "Strict-Transport-Security" in content
        assert "script-src 'self'" in content


def test_nginx_index_location_preserves_security_headers():
    for template in (NGINX_TEMPLATE, NGINX_SSL_TEMPLATE):
        content = template.read_text()
        match = re.search(r"location\s+=\s+/index\.html\s+\{(?P<body>.*?)\n\s*\}", content, flags=re.S)
        assert match, template.name
        body = match.group("body")
        assert "add_header_inherit merge" in body
        assert 'Cache-Control "no-store" always' in body
        assert "Content-Security-Policy" in body
        assert "Strict-Transport-Security" in body
        assert "X-Frame-Options" in body
        assert "X-Content-Type-Options" in body


def test_frontend_build_output_does_not_contain_secret_like_literals():
    build_dir = REPO_ROOT / "frontend" / "build"
    if not build_dir.exists():
        pytest.skip("frontend build output not present")

    suspicious = re.compile(
        r"(BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY|"
        r"AKIA[0-9A-Z]{16}|"
        r"-----BEGIN|"
        r"refresh_token['\"]?\s*[:=]\s*['\"][^'\"]{12,}|"
        r"password['\"]?\s*[:=]\s*['\"][^'\"]{8,}|"
        r"secret['\"]?\s*[:=]\s*['\"][^'\"]{8,})",
        re.I,
    )
    checked = []
    for path in build_dir.rglob("*"):
        if path.suffix.lower() not in {".js", ".css", ".html", ".json"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        checked.append(path)
        assert not suspicious.search(text), path
    assert checked
