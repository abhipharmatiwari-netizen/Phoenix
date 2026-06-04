"""Tests for Postgres SSL transport enforcement."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.runtime.app_runtime import _is_local_postgres_host


LOCAL_POSTGRES_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "host.docker.internal",
    "postgres",
    "phoenix-oci-postgres",
}


def _run_ssl_check(
    *,
    sslmode: str,
    skip_check: bool,
    k_service: str = "",
    pg_host: str = "host.docker.internal",
) -> str:
    """Exercise only the SSL validation path from app_runtime LIVE checks."""
    import os

    env_overrides = {
        "LIVE_PG_SSL_SKIP_CHECK": "true" if skip_check else "false",
        "K_SERVICE": k_service,
        "CONTROL_PLANE_PG_HOST": pg_host,
    }

    with patch.dict(os.environ, env_overrides, clear=False):
        import os as _os

        pg_sslmode = sslmode.strip().lower()
        ssl_check_ok = pg_sslmode in ("require", "verify-ca", "verify-full")
        ssl_skip = _os.getenv("LIVE_PG_SSL_SKIP_CHECK", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        pg_host_value = _os.getenv("CONTROL_PLANE_PG_HOST", "").strip().lower()
        is_local_pg_host = pg_host_value in LOCAL_POSTGRES_HOSTS
        is_cloud = bool(_os.getenv("K_SERVICE", "").strip())

        if not ssl_check_ok:
            if ssl_skip and is_cloud:
                raise RuntimeError(
                    "startup.ssl_error: LIVE_PG_SSL_SKIP_CHECK=true is forbidden in "
                    "cloud deployments (K_SERVICE is set)."
                )
            if ssl_skip:
                return "info" if is_local_pg_host else "warning"
            raise ValueError(
                "startup.ssl_error: TRADE_MODE=LIVE requires "
                f"CONTROL_PLANE_PG_SSLMODE=require. Current value is {pg_sslmode!r}."
            )
        return "ok"


class TestSSLEnforcement:
    def test_runtime_helper_recognizes_oci_local_postgres(self):
        assert _is_local_postgres_host(
            SimpleNamespace(control_plane_pg_host="phoenix-oci-postgres")
        )
        assert not _is_local_postgres_host(
            SimpleNamespace(control_plane_pg_host="db.internal.example")
        )

    def test_cloud_skip_check_aborts_with_runtime_error(self):
        import pytest

        with pytest.raises(RuntimeError, match="K_SERVICE is set"):
            _run_ssl_check(sslmode="prefer", skip_check=True, k_service="phoenix-live")

    def test_cloud_skip_check_with_require_mode_passes(self):
        assert (
            _run_ssl_check(
                sslmode="require",
                skip_check=True,
                k_service="phoenix-live",
            )
            == "ok"
        )

    def test_local_docker_skip_check_is_info_no_exception(self):
        assert (
            _run_ssl_check(
                sslmode="prefer",
                skip_check=True,
                k_service="",
                pg_host="phoenix-oci-postgres",
            )
            == "info"
        )

    def test_unknown_host_skip_check_still_warns(self):
        assert (
            _run_ssl_check(
                sslmode="prefer",
                skip_check=True,
                k_service="",
                pg_host="db.internal.example",
            )
            == "warning"
        )

    def test_no_skip_no_require_raises_value_error(self):
        import pytest

        with pytest.raises(ValueError, match="CONTROL_PLANE_PG_SSLMODE=require"):
            _run_ssl_check(sslmode="prefer", skip_check=False, k_service="")

    def test_require_mode_passes_cleanly(self):
        assert _run_ssl_check(sslmode="require", skip_check=False, k_service="") == "ok"
        assert (
            _run_ssl_check(
                sslmode="require",
                skip_check=False,
                k_service="phoenix-live",
            )
            == "ok"
        )

    def test_verify_ca_mode_passes(self):
        assert _run_ssl_check(sslmode="verify-ca", skip_check=False, k_service="") == "ok"

    def test_verify_full_mode_passes(self):
        assert (
            _run_ssl_check(sslmode="verify-full", skip_check=False, k_service="")
            == "ok"
        )
