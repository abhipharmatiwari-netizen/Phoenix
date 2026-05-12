from __future__ import annotations

from typing import Any, Optional
import logging
import os
import re
import socket
import time

try:  # pragma: no cover - optional in lightweight test environments
    import psycopg  # type: ignore
    from psycopg.conninfo import conninfo_to_dict, make_conninfo  # type: ignore
except Exception:  # pragma: no cover - optional in lightweight test environments
    psycopg = None  # type: ignore[assignment]

    def conninfo_to_dict(dsn: str) -> dict[str, str]:
        return {"dsn": dsn}

    def make_conninfo(**params: object) -> str:
        return " ".join(f"{key}={value}" for key, value in params.items() if value not in (None, ""))

from app.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_DEFAULT_POSTGRES_SESSION_TIMEZONE = "Asia/Kolkata"
_LEGACY_CALCUTTA_TZ_RE = re.compile(r"(?i)\bAsia/Calcutta\b")

def _build_dsn_from_parts(
    *,
    host: str,
    port: int,
    dbname: str,
    user: str,
    password: str,
    sslmode: Optional[str],
) -> str:
    params = {
        "host": host,
        "port": port,
        "dbname": dbname,
        "user": user,
        "password": password,
    }
    if sslmode:
        params["sslmode"] = sslmode
    return make_conninfo(**params)


def _require_control_plane_parts(settings: Settings) -> tuple[str, int, str, str, str, Optional[str]]:
    host = settings.control_plane_pg_host
    dbname = settings.control_plane_pg_db
    user = settings.control_plane_pg_user
    password = settings.control_plane_pg_password
    missing = []
    if not host:
        missing.append("CONTROL_PLANE_PG_HOST")
    if not dbname:
        missing.append("CONTROL_PLANE_PG_DB")
    if not user:
        missing.append("CONTROL_PLANE_PG_USER")
    if not password:
        missing.append("CONTROL_PLANE_PG_PASSWORD")
    if missing:
        raise RuntimeError(
            "Missing control plane Postgres settings: " + ", ".join(missing)
        )
    port = settings.control_plane_pg_port or 5432
    return host, port, dbname, user, password, settings.control_plane_pg_sslmode


def get_control_plane_dsn(settings: Optional[Settings] = None) -> str:
    settings = settings or get_settings()
    if settings.control_plane_db_dsn:
        return settings.control_plane_db_dsn
    host, port, dbname, user, password, sslmode = _require_control_plane_parts(settings)
    return _build_dsn_from_parts(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
        sslmode=sslmode,
    )


def get_sweep_state_dsn(settings: Optional[Settings] = None) -> str:
    settings = settings or get_settings()
    if settings.sweep_state_db_dsn:
        return settings.sweep_state_db_dsn
    return get_control_plane_dsn(settings)


def connect_with_retry(
    dsn: str,
    *,
    autocommit: bool = True,
    max_attempts: int = 3,
    base_backoff_seconds: float = 0.5,
    connect_timeout_seconds: Optional[int] = None,
) -> Any:
    """
    Connect to Postgres with a small retry/backoff and a configurable connect timeout.

    ``connect_timeout_seconds`` overrides the ``POSTGRES_CONNECT_TIMEOUT_SECONDS``
    env var for callers that need a tighter per-attempt budget than the
    fleet-wide default (e.g. issues #245/#246 — the kill-switch bridge
    must bound its total retry budget to < 10 seconds so emergency
    square-off in ``evaluate_account_loss`` is not blocked).
    """
    if psycopg is None:
        raise RuntimeError("psycopg is required for Postgres operations but is not installed")
    if connect_timeout_seconds is not None:
        try:
            connect_timeout = max(1, int(connect_timeout_seconds))
        except Exception:
            connect_timeout = 5
    else:
        try:
            connect_timeout = int(os.getenv("POSTGRES_CONNECT_TIMEOUT_SECONDS", "5"))
        except Exception:
            connect_timeout = 5
    connect_options = _postgres_connect_options()
    dsn_candidates = _dsn_connect_candidates(dsn)
    last_exc: Optional[Exception] = None
    attempts = max(1, int(max_attempts))
    for attempt in range(attempts):
        for candidate_idx, (candidate_dsn, candidate_label) in enumerate(dsn_candidates):
            try:
                connect_kwargs: dict[str, object] = {
                    "autocommit": autocommit,
                    "connect_timeout": connect_timeout,
                }
                if connect_options:
                    connect_kwargs["options"] = connect_options
                return psycopg.connect(
                    candidate_dsn,
                    **connect_kwargs,
                )
            except Exception as exc:
                last_exc = exc
                last_candidate = candidate_idx == (len(dsn_candidates) - 1)
                if (not last_candidate) and attempt < attempts:
                    logger.warning(
                        "Postgres connect attempt %d/%d via %s failed: %s; trying fallback route",
                        attempt + 1,
                        attempts,
                        candidate_label,
                        _compact_error(exc),
                    )
                    continue
                if attempt >= attempts - 1:
                    raise
                sleep_seconds = base_backoff_seconds * (2 ** attempt)
                logger.warning(
                    "Postgres connect attempt %d/%d via %s failed: %s; retrying in %.1fs",
                    attempt + 1,
                    attempts,
                    candidate_label,
                    _compact_error(exc),
                    sleep_seconds,
                )
                time.sleep(sleep_seconds)
                break
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Postgres connect failed without exception")


def _compact_error(exc: Exception, limit: int = 220) -> str:
    text = " ".join(str(exc).strip().split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...(truncated)"


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _resolve_first_ipv4(host: str) -> Optional[str]:
    try:
        infos = socket.getaddrinfo(
            host,
            None,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    except Exception:
        return None
    for info in infos:
        addr = info[4][0] if len(info) >= 5 else None
        if addr:
            return str(addr)
    return None


def _dsn_connect_candidates(dsn: str) -> list[tuple[str, str]]:
    try:
        params = conninfo_to_dict(dsn)
    except Exception:
        return [(dsn, "primary")]
    host = str(params.get("host") or "").strip()
    hostaddr = str(params.get("hostaddr") or "").strip()
    if not host or hostaddr:
        return [(dsn, host or "primary")]

    host_lower = host.lower()
    force_ipv4 = _env_truthy("POSTGRES_FORCE_IPV4", default=False)
    prefer_ipv4 = force_ipv4 or host_lower == "host.docker.internal"
    if not prefer_ipv4:
        return [(dsn, host)]

    ipv4 = _resolve_first_ipv4(host)
    if not ipv4:
        return [(dsn, host)]

    params_ipv4 = dict(params)
    params_ipv4["hostaddr"] = ipv4
    try:
        ipv4_dsn = make_conninfo(**params_ipv4)
    except Exception:
        return [(dsn, host)]
    return [
        (ipv4_dsn, f"{host}[ipv4={ipv4}]"),
        (dsn, host),
    ]


def _normalized_timezone(value: str) -> str:
    cleaned = str(value or "").strip() or _DEFAULT_POSTGRES_SESSION_TIMEZONE
    if _LEGACY_CALCUTTA_TZ_RE.fullmatch(cleaned):
        return _DEFAULT_POSTGRES_SESSION_TIMEZONE
    return cleaned


def _postgres_session_timezone() -> str:
    raw = (
        os.getenv("POSTGRES_SESSION_TIMEZONE")
        or os.getenv("DEFAULT_TIME_ZONE")
        or _DEFAULT_POSTGRES_SESSION_TIMEZONE
    )
    return _normalized_timezone(raw)


def _postgres_connect_options() -> str:
    base_options = str(os.getenv("POSTGRES_CONNECT_OPTIONS", "")).strip()
    timezone = _postgres_session_timezone()
    if not base_options:
        return f"-c timezone={timezone}"
    normalized = _LEGACY_CALCUTTA_TZ_RE.sub(_DEFAULT_POSTGRES_SESSION_TIMEZONE, base_options)
    if "timezone=" in normalized.lower():
        return normalized
    return f"{normalized} -c timezone={timezone}"


__all__ = [
    "get_control_plane_dsn",
    "get_sweep_state_dsn",
    "connect_with_retry",
]
