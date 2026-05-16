from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


def _oci_compose() -> dict:
    return yaml.safe_load(_read("docker-compose.oci-live.yml"))


def test_start_phoenix_preflights_backend_and_nginx_images_before_compose() -> None:
    script = _read("scripts/start-phoenix.sh")

    assert "docker image inspect \"$image\"" in script
    assert 'require_local_image "backend" "$BACKEND_IMAGE"' in script
    assert 'require_local_image "nginx" "$NGINX_IMAGE"' in script
    assert "phoenix-local-backend:${IMAGE_TAG}" in script
    assert "phoenix-local-nginx:${IMAGE_TAG}" in script
    assert "${IMAGE_BASE}/backend:${IMAGE_TAG}" in script
    assert "${IMAGE_BASE}/nginx:${IMAGE_TAG}" in script

    first_preflight = script.index('require_local_image "backend" "$BACKEND_IMAGE"')
    first_compose_start = script.index("docker compose")
    assert first_preflight < first_compose_start


def test_stop_phoenix_verifies_nginx_state_after_working_day_stop() -> None:
    script = _read("scripts/stop-phoenix.sh")

    assert "nginx should remain up" in script
    assert "sleep 30" in script
    assert "Verified: nginx still running." in script
    assert "ALERT: nginx is NOT running after backend stop." in script
    assert "nginx remains up" not in script


def test_backend_watchdog_is_observe_only_without_docker_socket_or_actions() -> None:
    compose = _read("docker-compose.oci-live.yml")
    watchdog = compose.split("  backend-watchdog:", 1)[1].split("\n# Secrets", 1)[0]

    assert "observe-only" in watchdog
    assert "/var/run/docker.sock" not in watchdog
    assert "docker stop phoenix-oci-web" not in watchdog
    assert "docker start phoenix-oci-web" not in watchdog
    assert "relying on LB drain" in watchdog


def test_optimizer_service_is_profile_only_one_shot_using_backend_image() -> None:
    compose = _oci_compose()
    services = compose["services"]
    optimizer = services["optimizer"]

    assert optimizer["profiles"] == ["optimizer"]
    assert optimizer["restart"] == "no"
    assert optimizer["image"].endswith("/phoenix-prod/backend:${IMAGE_TAG:?Set IMAGE_TAG to a specific git SHA - never deploy latest}")
    assert optimizer["depends_on"]["db-preflight"]["condition"] == "service_completed_successfully"
    assert "ports" not in optimizer
    assert optimizer["entrypoint"] == [
        "docker-entrypoint.sh",
        "python",
        "-m",
        "app.strategies.run_multi_strategy_optimizer",
    ]
    assert "--promote-to-candidate" in optimizer["command"]
    assert "--output=/app/optimizer_output/latest-results.json" in optimizer["command"]
    assert "control_plane_pg_password" in optimizer["secrets"]
    assert any(
        "/opt/phoenix/optimizer/output" in volume
        and ":/app/optimizer_output" in volume
        for volume in optimizer["volumes"]
    )
    assert optimizer["mem_limit"] == "1500m"
    assert optimizer["cap_drop"] == ["ALL"]


def test_oci_build_scripts_build_backend_and_nginx_image_pair() -> None:
    for rel_path in (
        "scripts/ops/build_and_push_image.sh",
        "scripts/ops/build_push_ip.sh",
    ):
        script = _read(rel_path)
        assert "/backend:${GIT_SHA}" in script
        assert "/nginx:${GIT_SHA}" in script
        assert '${APP_DIR}/Dockerfile' in script
        assert '${APP_DIR}/nginx/Dockerfile' in script


def test_redeploy_pulls_and_recreates_nginx_with_backend() -> None:
    script = _read("scripts/ops/redeploy_backend.sh")

    assert "pull backend nginx" in script
    assert "up -d --no-deps --no-build --force-recreate backend" in script
    assert "up -d --no-deps --no-build --force-recreate nginx" in script
    assert "docker inspect phoenix-oci-backend" in script
    assert "docker inspect phoenix-oci-web" in script


def test_oci_runbook_documents_image_pair_and_observe_only_watchdog() -> None:
    runbook = _read("docs/runbooks/oci_live_deployment.md")

    assert "Build the backend and nginx" in runbook
    assert "backend and nginx images are a release pair" in runbook
    assert "refuses to run `docker compose up` if either image is missing" in runbook
    assert "backend-watchdog` service is observe-only" in runbook
    assert "Nightly Optimizer One-Shot" in runbook
    assert "/opt/phoenix/optimizer/output" in runbook
    assert "--profile optimizer" in runbook
    assert "run --rm optimizer --help" in runbook
