"""Static checks for the SQL migration runner contract."""

from __future__ import annotations

import re
from pathlib import Path


_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_SCHEMA_MIGRATIONS_INSERT_RE = re.compile(
    r"\bINSERT\s+INTO\s+(?:public\.)?schema_migrations\b",
    flags=re.IGNORECASE,
)


def test_migrations_do_not_self_record_schema_migrations() -> None:
    offenders: list[str] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        if _SCHEMA_MIGRATIONS_INSERT_RE.search(sql):
            offenders.append(path.name)

    assert not offenders, (
        "Migration files must not write public.schema_migrations directly; "
        "scripts/run_migrations.sh records filename/checksum/app metadata after "
        f"each migration succeeds. Offenders: {offenders}"
    )
