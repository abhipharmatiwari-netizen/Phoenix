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
    assert "Backend /readyz status" in script
    assert "Phoenix /readyz is green and ready for trading." in script

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


def test_backend_watchdog_base_manifest_has_no_docker_socket_or_actions() -> None:
    compose = _read("docker-compose.oci-live.yml")
    watchdog = compose.split("  backend-watchdog:", 1)[1].split("\n# Secrets", 1)[0]

    assert "base-manifest polling" in watchdog
    assert "/var/run/docker.sock" not in watchdog
    assert "docker stop phoenix-oci-web" not in watchdog
    assert "docker start phoenix-oci-web" not in watchdog
    assert "no nginx action in base manifest" in watchdog


def test_oci_container_healthchecks_use_liveness_not_readiness() -> None:
    compose = _oci_compose()
    services = compose["services"]

    assert services["backend"]["healthcheck"]["test"] == [
        "CMD",
        "curl",
        "-f",
        "http://localhost:8080/health",
    ]
    nginx_probe = " ".join(services["nginx"]["healthcheck"]["test"])
    assert "https://127.0.0.1:8443/health" in nginx_probe
    assert "/readyz" not in nginx_probe


def test_oci_override_template_nginx_healthcheck_uses_liveness() -> None:
    override = _read("phoenix-override.yml.example")
    healthcheck_block = override.split("healthcheck:", 1)[1].split("ports:", 1)[0]

    assert "https://127.0.0.1:8443/health" in healthcheck_block
    assert "/readyz" not in healthcheck_block


def test_oi_ml_shadow_compose_does_not_blank_proxy_env_file_values() -> None:
    compose = yaml.safe_load(_read("ops/compose/docker-compose.oi-ml-shadow.yml"))
    sidecar = compose["services"]["oi-ml-shadow"]
    environment = sidecar["environment"]

    assert "/opt/phoenix/phoenix-deploy.env" in sidecar["env_file"]
    assert "ANGEL_HTTPS_PROXY" not in environment
    assert "HTTPS_PROXY" not in environment


def test_optimizer_service_is_profile_only_one_shot_using_backend_image() -> None:
    compose = _oci_compose()
    services = compose["services"]
    optimizer = services["optimizer"]

    assert optimizer["profiles"] == ["optimizer"]
    assert optimizer["restart"] == "no"
    assert optimizer["image"].endswith("/phoenix-prod/backend:${IMAGE_TAG:?Set IMAGE_TAG to a specific git SHA - never deploy latest}")
    assert "depends_on" not in optimizer
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


def test_optimizer_systemd_timer_and_service_are_profile_scoped() -> None:
    service = _read("ops/systemd/phoenix-optimizer.service")
    timer = _read("ops/systemd/phoenix-optimizer.timer")
    installer = _read("scripts/install-optimizer-systemd.sh")
    logrotate = _read("ops/logrotate/phoenix-optimizer")

    assert "ExecStartPre=/opt/phoenix/app/scripts/optimizer-precheck.sh" in service
    assert "/usr/bin/flock -n /opt/phoenix/state/optimizer.lock" in service
    assert "--profile optimizer run --rm optimizer" in service
    assert "Environment=CONTROL_PLANE_PG_PASSWORD_HOST=dummy" in service
    assert "StandardOutput=append:/opt/phoenix/logs/optimizer.log" in service
    assert "OnCalendar=*-*-* 23:45:00 Asia/Kolkata" in timer
    assert "Persistent=true" in timer
    assert "systemctl enable --now phoenix-optimizer.timer" in installer
    assert "chown 100:100 /opt/phoenix/optimizer/output" in installer
    assert "ln -sfn \"$APP_DIR/ops/systemd/phoenix-optimizer.service\"" in installer
    assert "/opt/phoenix/logs/optimizer.log" in logrotate
    assert "copytruncate" in logrotate


def test_optimizer_precheck_fails_closed_for_market_hours_orders_and_lock() -> None:
    script = _read("scripts/optimizer-precheck.sh")

    assert "09:00-15:35 IST" in script
    assert "flock -n \"$LOCK_FILE\" true" in script
    assert "docker logs --since 5m phoenix-oci-backend" in script
    assert "order_placed|ORDER_PLACED" in script
    assert "optimizer blocked" in script


def test_backend_reload_timer_is_conditional_and_fail_closed() -> None:
    script = _read("scripts/backend-reload-if-needed.sh")
    service = _read("ops/systemd/phoenix-backend-reload.service")
    timer = _read("ops/systemd/phoenix-backend-reload.timer")
    installer = _read("scripts/install-backend-reload-systemd.sh")
    logrotate = _read("ops/logrotate/phoenix-backend-reload")

    assert "OPTIMIZER_BACKEND_RELOAD_DISABLED=true" in script
    assert "status = 'promoted'" in script
    assert "Asia/Kolkata" in script
    assert "BACKEND_RELOAD_LOCK_HELD=true flock -n \"$LOCK_FILE\" \"$0\" \"$@\"" in script
    assert "no promoted candidates from the previous IST day; backend reload skipped" in script
    assert 'request_json("/admin/broker-accounts")' in script
    assert "/tenant/me/accounts/{account_path}/positions" in script
    assert "/tenant/me/accounts/{account_path}/orders" in script
    assert "TERMINAL_ORDER_STATUSES" in script
    assert "open positions or orders detected; refusing backend reload" in script
    assert "restart backend" in script
    assert "within 60s after reload" in script
    assert "docker inspect -f" in script

    assert "/usr/bin/flock -n /opt/phoenix/state/backend-reload.lock" in service
    assert "Environment=CONTROL_PLANE_PG_PASSWORD_HOST=dummy" in service
    assert "Environment=BACKEND_RELOAD_LOCK_HELD=true" in service
    assert "StandardOutput=append:/opt/phoenix/logs/backend-reload.log" in service
    assert "OnCalendar=*-*-* 09:00:00 Asia/Kolkata" in timer
    assert "Persistent=true" in timer
    assert "systemctl enable --now phoenix-backend-reload.timer" in installer
    assert "ln -sfn \"$APP_DIR/ops/systemd/phoenix-backend-reload.service\"" in installer
    assert "/opt/phoenix/logs/backend-reload.log" in logrotate
    assert "copytruncate" in logrotate


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
    assert "http://localhost:8080/health" in script
    assert "Backend /readyz status" in script
    assert "ALLOW_NON_READYZ_DEPLOY=true" in script
    assert "up -d --no-deps --no-build --force-recreate nginx" in script
    assert "docker inspect phoenix-oci-backend" in script
    assert "docker inspect phoenix-oci-web" in script


def test_oci_runbook_documents_verified_vm_runtime() -> None:
    runbook = _read("docs/runbooks/oci_live_deployment.md")

    assert "phoenix-local-backend:local-c8c80ea" in runbook
    assert "phoenix-local-nginx:local-4f567bf" in runbook
    assert "frontend-only" in runbook
    assert "static asset redeploy" in runbook
    assert "phoenix-oci-postgres" in runbook
    assert "Docker health status `healthy`" in runbook
    assert "VM-local Postgres" in runbook
    assert "source-file bind mounts" in runbook
    assert "observe-only" in runbook
    assert "Docker socket mounts or nginx stop/start logs indicate stale VM wiring" in runbook
    assert "optimizer and backend-reload systemd timers" in runbook
    assert "not current" in runbook


def test_oi_ml_rollout_runbook_pins_promotion_and_rollback_gates() -> None:
    runbook = _read("docs/runbooks/oi_ml_ce_seller_rollout.md")

    assert "40 clean sessions" in runbook
    assert "profit factor >= 1.25" in runbook
    assert "max simulated drawdown <= 6%" in runbook
    assert "10 complete sessions" in runbook
    assert "One spread max" in runbook
    assert "20 sessions" in runbook
    assert "Two spreads max" in runbook
    assert "allow_naked=false" in runbook
    assert "strict intraday" in runbook
    assert "Do not disable strict intraday" in runbook
    assert "Broker margin" in runbook
    assert "Kill-switch dry run" in runbook
    assert "Break-glass flatten drill" in runbook
    assert "Do not paste account numbers or secrets" in runbook
