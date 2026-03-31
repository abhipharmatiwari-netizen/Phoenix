from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class PositionsStatus(str, Enum):
    OK = "OK"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


@dataclass
class PositionsFetchResult:
    status: PositionsStatus
    positions: Optional[list]
    reason: str
    http_status: Optional[int] = None
    retry_after_seconds: Optional[float] = None
    raw_snippet: Optional[str] = None


__all__ = ["PositionsFetchResult", "PositionsStatus"]
