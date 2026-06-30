from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_COMPOSE_PATH = REPO_ROOT / "docker-compose.live.single.yml"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
NGINX_TEMPLATE_PATH = REPO_ROOT / "nginx" / "nginx.conf.template"
SECRETSTORE_LAUNCHER_PATH = REPO_ROOT / "start-docker-secretstore.ps1"
DOCKER_DESKTOP_RUNBOOK_PATH = REPO_ROOT / "docs" / "runbooks" / "docker_desktop_live_deployment.md"
VULTR_TUNNEL_ENTRYPOINT_PATH = REPO_ROOT / "scripts" / "ops" / "vultr_reverse_tunnel_entrypoint.sh"
VULTR_TUNNEL_FALLBACK_PATH = REPO_ROOT / "scripts" / "ops" / "start_vultr_reverse_tunnel.ps1"
VULTR_TUNNEL_TASK_PATH = REPO_ROOT / "scripts" / "ops" / "install_vultr_reverse_tunnel_task.ps1"


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


def test_vultr_tunnel_sidecar_uses_liveness_not_trading_readiness():
    compose_text = _read_text(LIVE_COMPOSE_PATH)
    entrypoint_text = _read_text(VULTR_TUNNEL_ENTRYPOINT_PATH)
    fallback_text = _read_text(VULTR_TUNNEL_FALLBACK_PATH)
    task_text = _read_text(VULTR_TUNNEL_TASK_PATH)

    assert "PHOENIX_TUNNEL_LIVENESS_URL" in compose_text
    assert "http://nginx/nginx-health" in compose_text
    assert "pidof ssh >/dev/null && curl -fsS http://nginx/nginx-health >/dev/null" in compose_text
    assert 'PHOENIX_TUNNEL_READY_URL: "${PHOENIX_TUNNEL_READY_URL:-http://nginx/readyz}"' not in compose_text
    assert "pidof ssh >/dev/null && curl -fsS http://nginx/readyz >/dev/null" not in compose_text

    assert "PHOENIX_TUNNEL_LIVENESS_URL" in entrypoint_text
    assert "http://nginx/nginx-health" in entrypoint_text
    assert "Phoenix liveness check failed" in entrypoint_text
    assert "PHOENIX_TUNNEL_READY_URL" not in entrypoint_text
    assert "http://nginx/readyz" not in entrypoint_text

    assert 'LocalLivenessPath = "/nginx-health"' in fallback_text
    assert "Local Phoenix liveness" in fallback_text
    assert "local Phoenix /readyz" not in task_text
    assert "local Phoenix nginx liveness" in task_text


def test_secretstore_launcher_builds_all_local_live_images_before_no_build_up():
    script_text = _read_text(SECRETSTORE_LAUNCHER_PATH)

    assert '@("docker", "compose", "-f", $composeFile, "build", "backend", "nginx", "vultr-tunnel")' in script_text
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


def test_secretstore_launcher_defaults_live_state_and_logs_outside_repo():
    script_text = _read_text(SECRETSTORE_LAUNCHER_PATH)

    assert "Set-DefaultHostPathOutsideRepo" in script_text
    assert "PHOENIX_STATE_HOST_PATH" in script_text
    assert "PHOENIX_LOG_HOST_PATH" in script_text
    assert "C:\\ProgramData\\phoenix\\state" in script_text
    assert "C:\\ProgramData\\phoenix\\logs" in script_text
    assert "LIVE Docker Desktop state and logs must be outside the checkout" in script_text
    assert "Copy-LegacyRiskStateIfMissing" in script_text
    assert "risk_positions.json.bak" in script_text


def test_docker_desktop_runbook_documents_secretstore_path_defaults():
    runbook_text = _read_text(DOCKER_DESKTOP_RUNBOOK_PATH)

    assert "Issue #388" in runbook_text
    assert "start-docker-secretstore.ps1` defaults `PHOENIX_STATE_HOST_PATH`" in runbook_text
    assert "PHOENIX_STATE_HOST_PATH=C:\\ProgramData\\phoenix\\state" in runbook_text
    assert "PHOENIX_LOG_HOST_PATH=C:\\ProgramData\\phoenix\\logs" in runbook_text
    assert "rejects any override that resolves inside the" in runbook_text
    assert "raw `docker compose up`" in runbook_text
    assert "phoenix-local-live" not in runbook_text
    assert "LEADER_LEASE_ID=phoenix-live-single-stack" in runbook_text


def test_docker_desktop_runbook_keeps_only_redacted_compose_evidence():
    runbook_text = _read_text(DOCKER_DESKTOP_RUNBOOK_PATH)

    assert "config > .\\compose.rendered.live.yml" not in runbook_text
    assert "docker compose -f .\\docker-compose.live.single.yml config --quiet" in runbook_text
    assert "compose.rendered.live.redacted.yml" in runbook_text
    assert "PGPASSWORD" in runbook_text
    assert "<redacted>" in runbook_text
    assert "Do not retain an unredacted `docker compose config` output." in runbook_text
