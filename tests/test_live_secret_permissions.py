from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_fetch_secrets_writes_owner_only_runtime_readable_files() -> None:
    script = (REPO_ROOT / "scripts" / "fetch-secrets.sh").read_text(encoding="utf-8")

    assert "admin_api_key|control_plane_pg_password" in script
    assert 'chown "${PHOENIX_SECRET_UID:-100}:${PHOENIX_SHARED_SECRET_GID:-0}" "$out_path"' in script
    assert 'chown "${PHOENIX_SECRET_UID:-100}:${PHOENIX_SECRET_GID:-101}" "$out_path"' in script
    assert 'chmod 440 "$out_path"' in script
    assert 'chmod 400 "$out_path"' in script
    assert 'chmod 644 "$out_path"' not in script


def test_live_secret_permission_validator_checks_mode_and_owner() -> None:
    script = (
        REPO_ROOT / "scripts" / "validate-live-secret-perms.sh"
    ).read_text(encoding="utf-8")

    assert 'EXPECTED_OWNER="${PHOENIX_SECRET_UID:-100}:${PHOENIX_SECRET_GID:-101}"' in script
    assert 'EXPECTED_SHARED_OWNER="${PHOENIX_SECRET_UID:-100}:${PHOENIX_SHARED_SECRET_GID:-0}"' in script
    assert 'expected_mode="440"' in script
    assert 'expected_mode="400"' in script
    assert 'expected $expected_owner' in script


def test_oci_file_hardening_script_does_not_print_secret_values() -> None:
    script = (
        REPO_ROOT / "scripts" / "ops" / "harden_oci_file_permissions.sh"
    ).read_text(encoding="utf-8")

    assert "cat " not in script
    assert "Get-Content" not in script
    assert "chmod 400" in script
    assert "chmod 440" in script
    assert "chmod 600" in script
    assert "validate-live-secret-perms.sh" in script


def test_oci_storage_report_is_non_destructive() -> None:
    script = (
        REPO_ROOT / "scripts" / "ops" / "oci_storage_report.sh"
    ).read_text(encoding="utf-8")

    assert "docker system df" in script
    assert "docker ps" in script
    assert "docker images" in script
    assert " prune" not in script
    assert " rm " not in script


def test_watchdog_recreate_verifies_socket_absent() -> None:
    script = (
        REPO_ROOT / "scripts" / "ops" / "recreate_oci_watchdog.sh"
    ).read_text(encoding="utf-8")

    assert "docker rm -f phoenix-oci-watchdog" in script
    assert "up -d --no-deps backend-watchdog" in script
    assert "/var/run/docker.sock" in script


def test_postgres_adoption_requires_explicit_confirmation_and_health() -> None:
    script = (
        REPO_ROOT / "scripts" / "ops" / "adopt_oci_postgres_compose.sh"
    ).read_text(encoding="utf-8")

    assert 'CONFIRM_POSTGRES_RECREATE:-}" != "YES"' in script
    assert "--profile vm-local-postgres" in script
    assert "docker rm phoenix-oci-postgres" in script
    assert ".State.Health.Status" in script
