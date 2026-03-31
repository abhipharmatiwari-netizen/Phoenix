"""Broker-specific typed error classes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class BrokerSchemaError(Exception):
    field: str
    keys: tuple[str, ...]
    payload_snippet: str
    message: str

    def __init__(
        self,
        *,
        field: str,
        keys: Iterable[str],
        payload_snippet: str,
        message: str | None = None,
    ) -> None:
        keys_tuple = tuple(str(k) for k in keys)
        text = message or (
            f"Broker schema validation failed for field={field} keys={list(keys_tuple)}"
        )
        object.__setattr__(self, "field", str(field))
        object.__setattr__(self, "keys", keys_tuple)
        object.__setattr__(self, "payload_snippet", str(payload_snippet or ""))
        object.__setattr__(self, "message", text)
        super().__init__(text)


__all__ = ["BrokerSchemaError"]
