from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_COMPOSE_PATH = REPO_ROOT / "docker-compose.live.single.yml"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
NGINX_TEMPLATE_PATH = REPO_ROOT / "nginx" / "nginx.conf.template"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_live_compose_enables_pending_lock_persistence():
    text = _read_text(LIVE_COMPOSE_PATH)
    assert 'OWNERSHIP_PERSIST_PENDING_LOCKS: "true"' in text


def test_live_compose_sets_non_empty_hub_instance_name_default():
    text = _read_text(LIVE_COMPOSE_PATH)
    match = re.search(
        r'HUB_INSTANCE_NAME:\s*"\$\{HUB_INSTANCE_NAME:-([^}]+)\}"',
        text,
    )
    assert match is not None
    assert match.group(1).strip() != ""


def test_live_compose_enables_postgres_leader_lease_single_stack_guard():
    text = _read_text(LIVE_COMPOSE_PATH)
    assert 'LEADER_LEASE_ENABLED: "true"' in text
    assert 'LEADER_LEASE_BACKEND: "postgres"' in text
    match = re.search(
        r'LEADER_LEASE_ID:\s*"\$\{LEADER_LEASE_ID:-([^}]+)\}"',
        text,
    )
    assert match is not None
    assert match.group(1).strip() == "phoenix-live-single-stack"


def test_live_deployment_probes_use_readyz():
    compose_text = _read_text(LIVE_COMPOSE_PATH)
    dockerfile_text = _read_text(DOCKERFILE_PATH)
    nginx_template_text = _read_text(NGINX_TEMPLATE_PATH)

    assert "/readyz" in dockerfile_text
    assert 'test: ["CMD", "curl", "-f", "http://localhost:8080/readyz"]' in compose_text
    assert 'test: ["CMD-SHELL", "wget -q -O - http://127.0.0.1/readyz >/dev/null 2>&1 || exit 1"]' in compose_text
    assert "location /readyz {" in nginx_template_text
