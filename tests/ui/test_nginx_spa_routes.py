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
