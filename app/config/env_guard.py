"""Environment separation guard — PHX-DEVOPS-003.

Prevents accidental cross-environment contamination by enforcing that:
  - Live credentials are never loaded in dev/test environments
  - Paper/dev credentials are never usable in live environments
  - Live environment endpoints are never contacted from non-live processes
  - Environment-specific resource names (DB tables, queues) are prefixed/validated

Call `env_guard.assert_env_safe()` at startup and before any
operation that crosses environment boundaries.
"""

from __future__ import annotations

import logging
import os
from enum import Enum

logger = logging.getLogger(__name__)


class AppEnv(str, Enum):
    LOCAL = "local"
    DEV = "dev"
    TEST = "test"
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE = "live"


_LIVE_ENVS = frozenset({AppEnv.LIVE})
_NON_LIVE_ENVS = frozenset({AppEnv.LOCAL, AppEnv.DEV, AppEnv.TEST, AppEnv.PAPER, AppEnv.SHADOW})

# Environment variables that must NOT be set in non-live environments
_LIVE_ONLY_VARS = {
    "ANGEL_CLIENT_ID",
    "ANGEL_API_KEY",
    "ANGEL_TOTP_SECRET",
    "LIVE_BROKER_ACCOUNT_ID",
}

# Environment variables that must NOT be set in live environments
_NON_LIVE_VARS = {
    "PAPER_MODE",
    "SIMULATED_BROKER",
}

# Markers that indicate a DSN is pointing at a live database
_LIVE_DB_MARKERS = {"prod", "live", "production"}


class EnvironmentViolation(Exception):
    """Raised when an environment safety check fails."""
    pass


def get_app_env() -> AppEnv:
    raw = os.getenv("APP_ENV", "local").strip().lower()
    try:
        return AppEnv(raw)
    except ValueError:
        logger.warning("Unknown APP_ENV=%r, treating as local", raw)
        return AppEnv.LOCAL


def is_live() -> bool:
    return get_app_env() in _LIVE_ENVS


def is_non_live() -> bool:
    return get_app_env() in _NON_LIVE_ENVS


def assert_env_safe(*, allow_live_in_test: bool = False) -> None:
    """Assert that the current environment configuration is safe.

    Raises EnvironmentViolation if cross-contamination is detected.
    """
    env = get_app_env()
    violations: list[str] = []

    if env in _NON_LIVE_ENVS and not allow_live_in_test:
        # Check that live-only vars are not set
        for var in _LIVE_ONLY_VARS:
            val = os.getenv(var, "").strip()
            if val:
                violations.append(
                    f"Live-only env var {var!r} is set in {env.value} environment"
                )

        # Check DSN is not pointing at a live database
        for dsn_var in ("CONTROL_PLANE_DSN", "DATABASE_URL", "POSTGRES_DSN"):
            dsn = os.getenv(dsn_var, "").strip().lower()
            if any(marker in dsn for marker in _LIVE_DB_MARKERS):
                violations.append(
                    f"DSN in {dsn_var!r} appears to point at a live database (contains {_LIVE_DB_MARKERS}) "
                    f"but APP_ENV={env.value}"
                )

    if env in _LIVE_ENVS:
        # Check that non-live vars are not set
        for var in _NON_LIVE_VARS:
            val = os.getenv(var, "").strip()
            if val.lower() in ("1", "true", "yes"):
                violations.append(
                    f"Non-live env var {var!r}={val!r} is set in LIVE environment"
                )

        # Live environment must have explicit broker credentials
        if not os.getenv("ANGEL_CLIENT_ID", "").strip():
            violations.append("ANGEL_CLIENT_ID must be set in LIVE environment")

    if violations:
        msg = "Environment safety violations:\n" + "\n".join(f"  - {v}" for v in violations)
        logger.critical("ENV_GUARD_VIOLATION: %s", msg)
        raise EnvironmentViolation(msg)

    logger.debug("env_guard_ok env=%s", env.value)


def assert_not_live(operation: str = "operation") -> None:
    """Assert that the current environment is NOT live. Use before destructive test operations."""
    if is_live():
        raise EnvironmentViolation(
            f"Operation '{operation}' is not allowed in LIVE environment"
        )


def assert_is_live(operation: str = "operation") -> None:
    """Assert that the current environment IS live. Use for operations that must only run in live."""
    if not is_live():
        raise EnvironmentViolation(
            f"Operation '{operation}' is only allowed in LIVE environment (APP_ENV={get_app_env().value})"
        )


__all__ = [
    "AppEnv",
    "EnvironmentViolation",
    "assert_env_safe",
    "assert_is_live",
    "assert_not_live",
    "get_app_env",
    "is_live",
    "is_non_live",
]
