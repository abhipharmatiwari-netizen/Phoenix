from pathlib import Path


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
