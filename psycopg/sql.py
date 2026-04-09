"""Tiny subset of psycopg.sql used by schema helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Composed:
    parts: tuple[object, ...]

    def __str__(self) -> str:
        return "".join(str(part) for part in self.parts)

    def format(self, *args: object) -> "Composed":
        rendered = str(self)
        for arg in args:
            rendered = rendered.replace("{}", str(arg), 1)
        return Composed((rendered,))

    def join(self, items: Iterable[object]) -> "Composed":
        rendered_items = [str(item) for item in items]
        if not rendered_items:
            return Composed(())
        separator = str(self)
        return Composed((separator.join(rendered_items),))


class SQL(Composed):
    def __init__(self, value: object):
        super().__init__((str(value),))


class Identifier(Composed):
    def __init__(self, value: object):
        escaped = str(value).replace('"', '""')
        super().__init__((f'"{escaped}"',))


__all__ = ["Composed", "Identifier", "SQL"]
