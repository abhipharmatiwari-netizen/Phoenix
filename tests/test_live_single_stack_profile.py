from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_COMPOSE_PATH = REPO_ROOT / "docker-compose.live.single.yml"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
NGINX_TEMPLATE_PATH = REPO_ROOT / "nginx" / "nginx.conf.template"
SECRETSTORE_LAUNCHER_PATH = REPO_ROOT / "start-docker-secretstore.ps1"


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


def test_live_compose_sslmode_default_is_require():
    """§105: compose default for CONTROL_PLANE_PG_SSLMODE must be 'require', not 'prefer'."""
    text = _read_text(LIVE_COMPOSE_PATH)
    # Both the x-pg-env and x-db-client-env blocks must default to require.
    assert ":-prefer}" not in text, (
        "Found ':-prefer}' in compose — CONTROL_PLANE_PG_SSLMODE or PGSSLMODE defaults "
        "to 'prefer'. Change the default to 'require' (issue #105)."
    )
    assert ":-require}" in text, (
        "Expected ':-require}' for CONTROL_PLANE_PG_SSLMODE default in compose (issue #105)."
    )


def test_live_container_healthchecks_use_liveness_not_readiness():
    compose_text = _read_text(LIVE_COMPOSE_PATH)
    dockerfile_text = _read_text(DOCKERFILE_PATH)
    nginx_template_text = _read_text(NGINX_TEMPLATE_PATH)

    assert "/readyz" in dockerfile_text
    assert 'test: ["CMD", "curl", "-f", "http://localhost:8080/health"]' in compose_text
    assert 'test: ["CMD-SHELL", "wget -q -O - http://127.0.0.1/nginx-health >/dev/null 2>&1 || exit 1"]' in compose_text
    assert "location = /readyz {" in nginx_template_text
    assert "location = /nginx-health {" in nginx_template_text
    assert "proxy_pass http://backend/readyz-public;" in nginx_template_text


def test_secretstore_launcher_builds_all_local_live_images_before_no_build_up():
    script_text = _read_text(SECRETSTORE_LAUNCHER_PATH)

    assert '@("docker", "compose", "-f", $composeFile, "build", "backend", "nginx")' in script_text
    assert '@("docker", "compose", "-f", $composeFile, "up", "-d", "--no-build", "--force-recreate")' in script_text


def test_secretstore_launcher_requires_postgres_account_specific_capital_limits():
    script_text = _read_text(SECRETSTORE_LAUNCHER_PATH)

    assert '$env:BROKER_SECRET_BACKEND = "postgres"' in script_text
    assert "Generic 5L/10L launcher defaults are not allowed for LIVE deployment" in script_text
    assert "Type YES to acknowledge and continue with generic limits" not in script_text
    assert "ALLOW_LIVE_CAPITAL_LIMITS_DEFAULT_ONLY=true found" not in script_text


def test_secretstore_launcher_redacts_capital_limits_json_output():
    script_text = _read_text(SECRETSTORE_LAUNCHER_PATH)

    assert 'if ($name -eq "CAPITAL_LIMITS_JSON")' in script_text
    assert "<present: redacted>" in script_text
