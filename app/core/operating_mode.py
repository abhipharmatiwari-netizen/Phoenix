"""OperatingMode enum and authority-path resolution (Architecture S1).

Defines the canonical operating modes and provides startup validation
to ensure exactly one authority path is active per contract/account scope.

Non-negotiable rules (S1):
  1. A contract may have one authoritative owner path at a time.
  2. Mixed write ownership is forbidden.
  5. Any ambiguity about the active authority path is a startup failure.

Authority is determined by the writer path only:
- hub-authoritative => ENABLE_MULTI_HUB=true and USE_HUB_ROUTER=true
- legacy-authoritative => ENABLE_MULTI_HUB=false and USE_HUB_ROUTER=false

The stream-worker flag is a separate market-data/runtime-plane concern. In the
current recommended LIVE profile the stream worker stays enabled while hub
services remain authoritative for order submission and state mutation.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


class OperatingMode(str, enum.Enum):
    """Canonical operating modes per Architecture S1."""

    LEGACY_AUTHORITATIVE = "LEGACY_AUTHORITATIVE"
    HUB_AUTHORITATIVE = "HUB_AUTHORITATIVE"

    @property
    def authority_path(self) -> str:
        """Return the canonical authority path string for this mode."""
        return "hub" if self == OperatingMode.HUB_AUTHORITATIVE else "legacy"


@dataclass(frozen=True)
class OperatingModeResolution:
    """Result of resolving the operating mode from runtime configuration.

    Attributes:
        mode: The resolved operating mode, or None if ambiguous in non-LIVE.
        authority_path: Canonical authority path string ("hub" or "legacy").
        reason: Human-readable explanation of how the mode was resolved.
    """

    mode: Optional[OperatingMode]
    authority_path: str
    reason: str

    @property
    def is_ambiguous(self) -> bool:
        """True if the mode could not be resolved unambiguously."""
        return self.mode is None

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value if self.mode is not None else None,
            "authority_path": self.authority_path,
            "reason": self.reason,
        }


def resolve_operating_mode_from_runtime(
    *,
    settings: Any,
    runtime_cfg: Any,
) -> OperatingModeResolution:
    """Resolve operating mode from runtime settings and config objects.

    Reads enable_multi_hub and use_hub_router from settings. The
    disable_stream_worker flag is carried through for logging, but it no longer
    determines authority path.

    Returns an OperatingModeResolution with the resolved mode and
    authority path. This function reports the tuple but does not itself
    enforce LIVE fail-closed behavior; startup_config_validator decides
    whether ambiguity is fatal.
    """
    enable_multi_hub = bool(getattr(settings, "enable_multi_hub", False))
    use_hub_router = bool(getattr(settings, "use_hub_router", False))
    disable_stream_worker = bool(getattr(runtime_cfg, "disable_stream_worker", False))

    try:
        mode = resolve_operating_mode(
            enable_multi_hub=enable_multi_hub,
            use_hub_router=use_hub_router,
            disable_stream_worker=disable_stream_worker,
        )
        return OperatingModeResolution(
            mode=mode,
            authority_path=mode.authority_path,
            reason=(
                f"Resolved from enable_multi_hub={enable_multi_hub}, "
                f"use_hub_router={use_hub_router}, "
                f"disable_stream_worker={disable_stream_worker} "
                "(stream plane only; authority resolved from hub/router flags)"
            ),
        )
    except ValueError:
        return OperatingModeResolution(
            mode=None,
            authority_path="unknown",
            reason=(
                f"Ambiguous: enable_multi_hub={enable_multi_hub}, "
                f"use_hub_router={use_hub_router}, "
                f"disable_stream_worker={disable_stream_worker} "
                "do not agree on a single authority path"
            ),
        )


def resolve_operating_mode(
    *,
    enable_multi_hub: bool,
    use_hub_router: bool,
    disable_stream_worker: bool = False,
) -> OperatingMode:
    """Resolve the operating mode from configuration flags.

    HUB_AUTHORITATIVE requires:
    - enable_multi_hub=True
    - use_hub_router=True

    LEGACY_AUTHORITATIVE requires:
    - enable_multi_hub=False
    - use_hub_router=False

    disable_stream_worker is intentionally ignored for authority resolution.
    Architecture treats it as a market-data/runtime-plane gate, not a writer
    authority selector.

    Any other combination raises ValueError (fail-closed).
    """
    if enable_multi_hub and use_hub_router:
        return OperatingMode.HUB_AUTHORITATIVE
    if not enable_multi_hub and not use_hub_router:
        return OperatingMode.LEGACY_AUTHORITATIVE
    raise ValueError(
        f"Ambiguous operating mode: enable_multi_hub={enable_multi_hub}, "
        f"use_hub_router={use_hub_router}, "
        f"disable_stream_worker={disable_stream_worker}. "
        "Architecture S1 requires enable_multi_hub and use_hub_router to agree "
        "on a single authority path: HUB_AUTHORITATIVE=(True,True), "
        "LEGACY_AUTHORITATIVE=(False,False). "
        "disable_stream_worker does not determine authority."
    )


def validate_operating_mode_at_startup(
    *,
    settings: Any,
    trade_mode: str,
    runtime_cfg: Any | None = None,
) -> Optional[OperatingMode]:
    """Validate and return the resolved operating mode.

    Uses enable_multi_hub/use_hub_router from settings. The
    disable_stream_worker value is accepted from runtime_cfg (or settings as a
    fallback) for compatibility and logging only; it does not decide authority.

    In LIVE mode, raises ValueError on ambiguity.
    In other modes, logs a warning and returns the best-effort mode.
    """
    enable_multi_hub = bool(getattr(settings, "enable_multi_hub", False))
    use_hub_router = bool(getattr(settings, "use_hub_router", False))
    disable_stream_worker_source = runtime_cfg if runtime_cfg is not None else settings
    disable_stream_worker = bool(
        getattr(disable_stream_worker_source, "disable_stream_worker", False)
    )

    try:
        mode = resolve_operating_mode(
            enable_multi_hub=enable_multi_hub,
            use_hub_router=use_hub_router,
            disable_stream_worker=disable_stream_worker,
        )
        logger.info(
            "Operating mode resolved: %s (enable_multi_hub=%s, use_hub_router=%s, disable_stream_worker=%s)",
            mode.value,
            enable_multi_hub,
            use_hub_router,
            disable_stream_worker,
        )
        return mode
    except ValueError:
        if str(trade_mode).strip().upper() == "LIVE":
            raise
        logger.warning(
            "Ambiguous operating mode in %s mode (non-fatal): "
            "enable_multi_hub=%s, use_hub_router=%s, disable_stream_worker=%s",
            trade_mode,
            enable_multi_hub,
            use_hub_router,
            disable_stream_worker,
        )
        return None


def assert_authority_path_match(
    *,
    current_mode: OperatingMode,
    caller_path: str,
    scope_key: str,
    action: str,
) -> None:
    """Block cross-authority mutations per Architecture S1 rule 1-2.

    Raises RuntimeError if the caller's authority path does not match
    the resolved operating mode.
    """
    expected = "hub" if current_mode == OperatingMode.HUB_AUTHORITATIVE else "legacy"
    if caller_path != expected:
        raise RuntimeError(
            f"Cross-authority mutation blocked: {action} on {scope_key} "
            f"by {caller_path!r} path, but operating mode is {current_mode.value} "
            f"(expected {expected!r}). Architecture S1 forbids mixed write ownership."
        )


__all__ = [
    "OperatingMode",
    "OperatingModeResolution",
    "assert_authority_path_match",
    "resolve_operating_mode",
    "resolve_operating_mode_from_runtime",
    "validate_operating_mode_at_startup",
]
