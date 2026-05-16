"""Unit tests for app/strategies/candidate_writer.py (issue #272).

Mocks psycopg via the ``connect_fn`` injection point — no real Postgres
required. The mock connection records every SQL executed so the tests
can assert the supersede-then-insert ordering and the JSONB / daterange
casts.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.strategies.candidate_writer import (
    CandidateBatch,
    CandidateWriter,
    CandidateWriterError,
    STRATEGY_NAME_TO_CONFIG_STRATEGY_ID,
    _resolve_optimizer_version,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, conn: "_FakeConnection") -> None:
        self._conn = conn
        self._last_sql: str = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._last_sql = sql
        self._conn.executed.append((sql.strip(), dict(params or {})))

    def fetchall(self) -> List[Tuple[Any, ...]]:
        # Return whatever the test scripted for the most recent SELECT.
        return self._conn.next_fetch.pop(0) if self._conn.next_fetch else []

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        # PR #288 codex round-6 P1: the writer probes
        # ``to_regclass('public.strategy_config_candidates')`` to fail
        # fast when migration 020 hasn't been applied. Tests can
        # script that single-row response via ``next_fetchone`` —
        # default is a healthy "table exists" reply so existing
        # tests don't have to declare it.
        if getattr(self._conn, "next_fetchone", None):
            return self._conn.next_fetchone.pop(0)
        return ("public.strategy_config_candidates",)

    @property
    def rowcount(self) -> int:
        # The supersede UPDATE is expected to set this so the writer can
        # log "superseded N rows". Tests script this via
        # ``conn.next_rowcount``.
        if self._conn.next_rowcount:
            return self._conn.next_rowcount.pop(0)
        return 0


class _FakeConnection:
    def __init__(self) -> None:
        self.executed: List[Tuple[str, Dict[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.next_fetch: List[List[Tuple[Any, ...]]] = []
        # PR #288 codex round-6 P1: optional scripted responses to
        # ``cur.fetchone()``. Default behaviour (when this list is
        # empty) is a "table exists" stub so the writer's schema
        # probe passes; tests for the missing-table failure path push
        # ``(None,)`` here.
        self.next_fetchone: List[Optional[Tuple[Any, ...]]] = []
        self.next_rowcount: List[int] = []
        self.closed = False
        # Trigger this to make .execute raise on next call.
        self.raise_on_next_execute: Optional[Exception] = None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _connect_fn_returning(conn: _FakeConnection):
    """Build a connect_fn that hands the same conn back on each call."""
    @contextmanager
    def _cm():
        yield conn

    def _factory():
        return _cm()

    return _factory


# ---------------------------------------------------------------------------
# Constructor / validation
# ---------------------------------------------------------------------------


def test_init_requires_tenant_and_account() -> None:
    with pytest.raises(CandidateWriterError):
        CandidateWriter(tenant_id="", broker_account_id="acc")
    with pytest.raises(CandidateWriterError):
        CandidateWriter(tenant_id="t", broker_account_id="")


def test_optimizer_version_resolves_from_image_tag(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_TAG", "abc1234")
    assert _resolve_optimizer_version() == "abc1234"


def test_optimizer_version_falls_back_to_unknown_when_git_fails(monkeypatch) -> None:
    monkeypatch.delenv("IMAGE_TAG", raising=False)
    import subprocess as _subprocess

    def _boom(*a, **kw):
        raise FileNotFoundError("git not installed")

    monkeypatch.setattr(_subprocess, "check_output", _boom)
    # Re-import so the module-level reference is patched too — the
    # writer module imports subprocess at module scope, so patching
    # subprocess.check_output works because the call site uses
    # ``subprocess.check_output(...)`` qualified name.
    assert _resolve_optimizer_version() == "unknown"


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_unknown_strategy_name_raises() -> None:
    conn = _FakeConnection()
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="not_a_real_strategy",
        underlying_label="NIFTY",
        top_candidates=[{"params": {}, "metrics": {}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    with pytest.raises(CandidateWriterError) as exc:
        writer.write_batch(batch)
    assert "unknown strategy_name" in str(exc.value)


def test_strategy_config_lookup_no_rows_raises() -> None:
    conn = _FakeConnection()
    conn.next_fetch.append([])  # SELECT returns nothing
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY",
        top_candidates=[{"params": {}, "metrics": {}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    with pytest.raises(CandidateWriterError) as exc:
        writer.write_batch(batch)
    msg = str(exc.value)
    assert "no strategy_configs row" in msg
    assert "ema20_strategy" in msg


def test_strategy_config_lookup_multiple_rows_picks_first_and_warns(caplog) -> None:
    conn = _FakeConnection()
    conn.next_fetch.append([("cfg-alpha", True), ("cfg-beta", True)])  # SELECT
    conn.next_rowcount.append(0)  # supersede affects 0 rows
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY",
        top_candidates=[{"params": {"k": 1}, "metrics": {"s": 1}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    with caplog.at_level("WARNING"):
        inserted = writer.write_batch(batch)
    assert len(inserted) == 1
    # The INSERT must reference the FIRST returned strategy_config_id.
    insert_args = next(
        a for sql, a in conn.executed if sql.startswith("INSERT INTO public.strategy_config_candidates")
    )
    assert insert_args["strategy_config_id"] == "cfg-alpha"
    assert any("strategy_configs rows match" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Insert / supersede semantics
# ---------------------------------------------------------------------------


def test_supersede_then_insert_runs_in_order_and_commits() -> None:
    conn = _FakeConnection()
    conn.next_fetch.append([("cfg-1", True)])
    conn.next_rowcount.append(0)
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="opt-sha",
        connect_fn=_connect_fn_returning(conn),
    )
    candidate = {
        "params": {"sl_pct": 0.30, "tp_pct": 0.25, "ema_period": 20},
        "metrics": {"score": 1234.5, "win_rate": 0.62, "total_pnl": 5678.0},
    }
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY",
        top_candidates=[candidate],
        backtest_window=(date(2026, 4, 14), date(2026, 5, 14)),
    )
    inserted = writer.write_batch(batch, candidates_per_strategy=1)
    assert len(inserted) == 1

    # Filter out the PR #288 codex round-6 P1 schema-readiness probe
    # so this test only asserts the application-level SQL sequence.
    sql_sequence = [
        sql for sql, _ in conn.executed
        if not sql.startswith("SELECT to_regclass")
    ]
    # Expected: SELECT strategy_config_id → UPDATE (supersede) → INSERT.
    assert sql_sequence[0].startswith("SELECT strategy_config_id")
    assert sql_sequence[1].startswith("UPDATE public.strategy_config_candidates")
    assert sql_sequence[2].startswith("INSERT INTO public.strategy_config_candidates")

    insert_args = next(
        a for sql, a in conn.executed
        if sql.startswith("INSERT INTO public.strategy_config_candidates")
    )
    assert insert_args["strategy_config_id"] == "cfg-1"
    assert insert_args["optimizer_version"] == "opt-sha"
    assert insert_args["candidate_id"] == inserted[0]
    # Daterange literal is inclusive on both ends — see CandidateWriter docstring.
    assert insert_args["backtest_window"] == "[2026-04-14,2026-05-14]"
    # params/metrics are serialized JSON strings (sorted keys for stability).
    assert '"sl_pct": 0.3' in insert_args["params"]
    assert '"win_rate": 0.62' in insert_args["metrics"]

    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_supersede_rowcount_is_logged(caplog) -> None:
    conn = _FakeConnection()
    conn.next_fetch.append([("cfg-1", True)])
    conn.next_rowcount.append(2)  # two prior pending rows superseded
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY",
        top_candidates=[{"params": {"k": 1}, "metrics": {"s": 1}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    with caplog.at_level("INFO"):
        writer.write_batch(batch)
    assert any("superseded 2 prior pending row(s)" in r.message for r in caplog.records)


def test_candidates_per_strategy_caps_inserts() -> None:
    conn = _FakeConnection()
    conn.next_fetch.append([("cfg-1", True)])
    # Two inserts → two supersede rowcounts queued
    conn.next_rowcount.extend([0, 0])
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    candidates = [
        {"params": {"k": i}, "metrics": {"score": float(10 - i)}} for i in range(5)
    ]
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY",
        top_candidates=candidates,
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    inserted = writer.write_batch(batch, candidates_per_strategy=2)
    assert len(inserted) == 2
    inserts = [a for sql, a in conn.executed if sql.startswith("INSERT INTO public.strategy_config_candidates")]
    assert len(inserts) == 2


def test_zero_candidates_per_strategy_writes_nothing() -> None:
    conn = _FakeConnection()
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY",
        top_candidates=[{"params": {"k": 1}, "metrics": {"s": 1}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    assert writer.write_batch(batch, candidates_per_strategy=0) == []
    # No connection used at all.
    assert conn.executed == []


def test_empty_top_candidates_warns_and_returns_empty(caplog) -> None:
    conn = _FakeConnection()
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY",
        top_candidates=[],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    with caplog.at_level("WARNING"):
        result = writer.write_batch(batch)
    assert result == []
    assert any("empty top_candidates" in r.message for r in caplog.records)


def test_invalid_backtest_window_raises() -> None:
    conn = _FakeConnection()
    conn.next_fetch.append([("cfg-1", True)])
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    # End precedes start.
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY",
        top_candidates=[{"params": {"k": 1}, "metrics": {"s": 1}}],
        backtest_window=(date(2026, 5, 14), date(2026, 5, 1)),
    )
    with pytest.raises(CandidateWriterError) as exc:
        writer.write_batch(batch)
    assert "precedes start" in str(exc.value)


def test_non_mapping_params_or_metrics_raise() -> None:
    conn = _FakeConnection()
    conn.next_fetch.append([("cfg-1", True)])
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY",
        top_candidates=[{"params": "not-a-dict", "metrics": {}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    with pytest.raises(CandidateWriterError):
        writer.write_batch(batch)


def test_rollback_called_when_insert_fails() -> None:
    # Connection where the INSERT raises.
    class _ExplodingCursor(_FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if sql.strip().startswith("INSERT INTO public.strategy_config_candidates"):
                raise RuntimeError("simulated insert failure")

    class _ExplodingConnection(_FakeConnection):
        def cursor(self):
            return _ExplodingCursor(self)

    conn = _ExplodingConnection()
    conn.next_fetch.append([("cfg-1", True)])
    conn.next_rowcount.append(0)
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY",
        top_candidates=[{"params": {"k": 1}, "metrics": {"s": 1}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    with pytest.raises(RuntimeError, match="simulated insert failure"):
        writer.write_batch(batch)
    assert conn.commits == 0
    assert conn.rollbacks == 1


# ---------------------------------------------------------------------------
# Strategy-id mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "short, canonical",
    list(STRATEGY_NAME_TO_CONFIG_STRATEGY_ID.items()),
)
def test_strategy_name_mapping_matches_canonical_strategy_id(short: str, canonical: str) -> None:
    conn = _FakeConnection()
    conn.next_fetch.append([(f"cfg-{canonical}", True)])
    conn.next_rowcount.append(0)
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name=short,
        underlying_label="NIFTY",
        top_candidates=[{"params": {"k": 1}, "metrics": {"s": 1}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    writer.write_batch(batch, candidates_per_strategy=1)
    select_args = next(
        a for sql, a in conn.executed if sql.startswith("SELECT strategy_config_id")
    )
    assert select_args["strategy_id"] == canonical


def test_strategy_id_overrides_take_precedence() -> None:
    conn = _FakeConnection()
    conn.next_fetch.append([("cfg-1", True)])
    conn.next_rowcount.append(0)
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
        strategy_id_overrides={"ema20": "custom_ema20_v2"},
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY",
        top_candidates=[{"params": {"k": 1}, "metrics": {"s": 1}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    writer.write_batch(batch, candidates_per_strategy=1)
    select_args = next(
        a for sql, a in conn.executed if sql.startswith("SELECT strategy_config_id")
    )
    assert select_args["strategy_id"] == "custom_ema20_v2"



# ---------------------------------------------------------------------------
# PR #288 codex round-3 regressions.
# ---------------------------------------------------------------------------


def test_lookup_prefers_enabled_strategy_config_over_disabled():
    """Codex round-3 P2: if both an enabled and a disabled strategy_configs
    row match (tenant, broker, strategy_id), pick the ENABLED one so
    promotion doesn't attach the candidate to a stale disabled config.
    """
    conn = _FakeConnection()
    # Simulate Postgres ORDER BY enabled DESC: enabled row first.
    conn.next_fetch.append([("cfg-b-enabled", True), ("cfg-a-disabled", False)])
    conn.next_rowcount.append(0)
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        top_candidates=[{"params": {"k": 1}, "metrics": {"s": 1}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    writer.write_batch(batch, candidates_per_strategy=1)
    insert_args = next(
        a for sql, a in conn.executed
        if sql.startswith("INSERT INTO public.strategy_config_candidates")
    )
    assert insert_args["strategy_config_id"] == "cfg-b-enabled"

    select_sql = next(
        sql for sql, _ in conn.executed if sql.startswith("SELECT strategy_config_id")
    )
    assert "ORDER BY enabled DESC" in select_sql


def test_lookup_falls_back_to_disabled_row_with_warning(caplog):
    """When ONLY disabled rows match, the writer still attaches the
    candidate so the operator can act later -- but logs a clear warning.
    """
    conn = _FakeConnection()
    conn.next_fetch.append([("cfg-disabled", False)])
    conn.next_rowcount.append(0)
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        top_candidates=[{"params": {"k": 1}, "metrics": {"s": 1}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    with caplog.at_level("WARNING"):
        writer.write_batch(batch, candidates_per_strategy=1)
    assert any(
        "only disabled strategy_configs rows" in r.message for r in caplog.records
    )


def test_supersede_filters_by_underlying_label():
    """Codex round-3 P2: identical params on different underlyings must
    not clobber each other's pending rows. The supersede UPDATE must
    include ``metrics->>'underlying_label' = <underlying>``.
    """
    conn = _FakeConnection()
    conn.next_fetch.append([("cfg-1", True)])
    conn.next_rowcount.append(0)
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        top_candidates=[{"params": {"k": 1}, "metrics": {"score": 1.0}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    writer.write_batch(batch, candidates_per_strategy=1)

    supersede_sql, supersede_args = next(
        (sql, a) for sql, a in conn.executed
        if sql.startswith("UPDATE public.strategy_config_candidates")
    )
    assert "metrics->>'underlying_label'" in supersede_sql
    assert supersede_args["underlying_label"] == "NIFTY_IDX"


def test_metrics_records_underlying_label_for_reviewers():
    """Reviewers need to know which underlying a candidate was scored
    on. ``underlying_label`` lands in the metrics JSONB even when the
    caller's ``metrics`` dict didn't include it.
    """
    conn = _FakeConnection()
    conn.next_fetch.append([("cfg-1", True)])
    conn.next_rowcount.append(0)
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="BANKNIFTY_IDX",
        top_candidates=[{"params": {"k": 1}, "metrics": {"score": 1.0}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    writer.write_batch(batch, candidates_per_strategy=1)
    insert_args = next(
        a for sql, a in conn.executed
        if sql.startswith("INSERT INTO public.strategy_config_candidates")
    )
    import json
    metrics_payload = json.loads(insert_args["metrics"])
    assert metrics_payload["underlying_label"] == "BANKNIFTY_IDX"



# ---------------------------------------------------------------------------
# PR #288 codex round-4 regressions.
# ---------------------------------------------------------------------------


def test_caller_supplied_underlying_label_is_overwritten():
    """Codex round-4 P2: a stale caller-supplied ``underlying_label`` in
    metrics (e.g. an ad-hoc tool reusing an old payload) must NOT
    survive -- the writer overwrites unconditionally with
    ``batch.underlying_label`` so the supersede match against
    ``metrics->>'underlying_label'`` can never miss.
    """
    conn = _FakeConnection()
    conn.next_fetch.append([("cfg-1", True)])
    conn.next_rowcount.append(0)
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        top_candidates=[
            {"params": {"k": 1}, "metrics": {"score": 1.0, "underlying_label": "STALE_OLD"}}
        ],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    writer.write_batch(batch, candidates_per_strategy=1)
    insert_args = next(
        a for sql, a in conn.executed
        if sql.startswith("INSERT INTO public.strategy_config_candidates")
    )
    import json
    metrics_payload = json.loads(insert_args["metrics"])
    assert metrics_payload["underlying_label"] == "NIFTY_IDX"


def test_promote_top_candidates_propagates_unexpected_failure(monkeypatch):
    """Codex round-4 P2: an infrastructure failure (missing table,
    connection drop mid-batch, etc.) must NOT be swallowed by the
    per-(strategy, underlying) try/except -- it must propagate so the
    nightly run exits non-zero and the operator sees the failure.
    """
    from app.strategies import run_multi_strategy_optimizer as rmso

    class _BoomWriter:
        optimizer_version = "v1"

        def write_batch(self, *a, **kw):
            raise RuntimeError("strategy_config_candidates table missing")

    monkeypatch.setattr(
        rmso, "CandidateWriter", lambda **kw: _BoomWriter()
    )

    results = {
        "ema20": {
            "NIFTY_IDX": {"top_5": [{"params": {"k": 1}, "metrics": {}}]},
        }
    }
    with pytest.raises(RuntimeError, match="strategy_config_candidates table missing"):
        rmso._promote_top_candidates(
            results=results,
            tenant_id="t",
            broker_account_id="a",
            lookback_days=20,
            candidates_per_strategy=1,
            dsn=None,
        )


def test_promote_top_candidates_still_skips_known_writer_errors(monkeypatch, caplog):
    """``CandidateWriterError`` (missing strategy_configs row, etc.) is
    still per-strategy-skipped -- only UNEXPECTED exceptions propagate.
    """
    from app.strategies import run_multi_strategy_optimizer as rmso

    class _MissingRowWriter:
        optimizer_version = "v1"

        def write_batch(self, *a, **kw):
            raise CandidateWriterError("no strategy_configs row")

    monkeypatch.setattr(
        rmso, "CandidateWriter", lambda **kw: _MissingRowWriter()
    )

    results = {
        "ema20": {
            "NIFTY_IDX": {"top_5": [{"params": {"k": 1}, "metrics": {}}]},
        }
    }
    with caplog.at_level("ERROR"):
        rmso._promote_top_candidates(
            results=results,
            tenant_id="t",
            broker_account_id="a",
            lookback_days=20,
            candidates_per_strategy=1,
            dsn=None,
        )
    assert any("no strategy_configs row" in r.message for r in caplog.records)


def test_promote_top_candidates_uses_supplied_loader_end_date():
    """Codex round-4 P2: when the caller captures the loader's end_date
    at backtest-start time and passes it to ``_promote_top_candidates``,
    the recorded ``backtest_window`` must be anchored on THAT date,
    not on a fresh ``datetime.now(IST).date()`` evaluated at promotion
    time (which can drift across IST midnight)."""
    from datetime import date as _date

    from app.strategies import run_multi_strategy_optimizer as rmso

    captured_window: list = []

    class _CaptureWriter:
        optimizer_version = "v1"

        def write_batch(self, batch, candidates_per_strategy):
            captured_window.append(batch.backtest_window)
            return ["cid-1"]

    rmso.CandidateWriter = lambda **kw: _CaptureWriter()
    try:
        results = {
            "ema20": {
                "NIFTY_IDX": {"top_5": [{"params": {"k": 1}, "metrics": {}}]},
            }
        }
        # Pin to a known historical date so we can assert the exact value.
        fixed = _date(2026, 5, 14)
        rmso._promote_top_candidates(
            results=results,
            tenant_id="t",
            broker_account_id="a",
            lookback_days=10,
            candidates_per_strategy=1,
            dsn=None,
            loader_end_date=fixed,
        )
    finally:
        # Restore the real CandidateWriter for other tests.
        from app.strategies.candidate_writer import CandidateWriter as _RealCW
        rmso.CandidateWriter = _RealCW

    assert captured_window, "writer was not invoked"
    start, end = captured_window[0]
    assert end == fixed, f"expected end={fixed!r}, got {end!r}"
    assert (end - start).days == 10



# ---------------------------------------------------------------------------
# PR #288 codex round-5 regressions.
# ---------------------------------------------------------------------------


def test_normalize_for_json_returns_native_python_types():
    """NumPy scalars must be cast to native Python types BEFORE
    ``json.dumps`` so the JSONB column stores real JSON values, not
    stringified ``"False"`` / ``"1.0"`` that would break the supersede
    match on the next run.
    """
    import numpy as np
    from app.strategies.candidate_writer import _normalize_for_json

    payload = {
        "require_rsi_falling": np.bool_(False),
        "ema_period": np.int64(20),
        "sl_pct": np.float64(0.30),
        "nested": {"flag": np.bool_(True)},
        "list_of_floats": [np.float64(1.5), np.float64(2.5)],
    }
    normalized = _normalize_for_json(payload)
    assert normalized["require_rsi_falling"] is False
    assert normalized["ema_period"] == 20 and isinstance(normalized["ema_period"], int)
    assert normalized["sl_pct"] == 0.30 and isinstance(normalized["sl_pct"], float)
    assert normalized["nested"]["flag"] is True
    assert normalized["list_of_floats"] == [1.5, 2.5]
    # And the result round-trips through json.dumps without falling
    # through to ``str()`` for any value (which would corrupt the
    # supersede match later).
    import json
    serialized = json.dumps(normalized, sort_keys=True)
    assert "false" in serialized  # JSON boolean, not "False"
    assert '"False"' not in serialized
    assert '"True"' not in serialized


def test_writer_serializes_numpy_booleans_as_json_booleans():
    """Round trip the writer end-to-end with a NumPy bool param and
    confirm the supersede WHERE clause sees JSON ``false`` (not
    stringified ``"False"`` -- that would break the supersede match
    against the next run's normalized params)."""
    import numpy as np

    conn = _FakeConnection()
    conn.next_fetch.append([("cfg-1", True)])
    conn.next_rowcount.append(0)
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        top_candidates=[{
            "params": {"require_rsi_falling": np.bool_(False), "sl_pct": np.float64(0.30)},
            "metrics": {"score": np.float64(1.0)},
        }],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    writer.write_batch(batch, candidates_per_strategy=1)

    insert_args = next(
        a for sql, a in conn.executed
        if sql.startswith("INSERT INTO public.strategy_config_candidates")
    )
    # The serialized JSON must use actual JSON booleans / numbers.
    assert '"require_rsi_falling": false' in insert_args["params"]
    assert '"sl_pct": 0.3' in insert_args["params"]
    assert '"False"' not in insert_args["params"]



# ---------------------------------------------------------------------------
# PR #288 codex round-6 regressions.
# ---------------------------------------------------------------------------


def test_writer_raises_clean_error_when_candidates_table_missing():
    """When migration 020 (PR #281) has not been applied yet, the
    writer must fail FAST with a clean ``CandidateWriterError`` that
    names the missing migration -- not a raw psycopg
    ``relation "..." does not exist``.

    PR #288 codex round-7 P1: the error must be the
    ``SchemaNotReadyError`` SUBCLASS (still a ``CandidateWriterError``
    so existing callers compile) so the orchestrator can distinguish
    infrastructure failure from per-pair misconfig and re-raise it.
    """
    from app.strategies.candidate_writer import SchemaNotReadyError

    conn = _FakeConnection()
    # to_regclass returns NULL when the table does not exist.
    conn.next_fetchone.append((None,))
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        top_candidates=[{"params": {"k": 1}, "metrics": {}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    with pytest.raises(SchemaNotReadyError) as exc:
        writer.write_batch(batch, candidates_per_strategy=1)
    # Must still satisfy ``isinstance(exc, CandidateWriterError)`` so
    # existing handlers that catch the parent class continue to work.
    assert isinstance(exc.value, CandidateWriterError)
    msg = str(exc.value)
    assert "strategy_config_candidates" in msg
    assert "migration" in msg.lower()
    # No INSERT should have been attempted.
    assert not any(
        sql.startswith("INSERT INTO public.strategy_config_candidates")
        for sql, _ in conn.executed
    )


def test_promote_orchestrator_lets_schema_not_ready_escape():
    """PR #288 codex round-7 P1: the per-(strategy, underlying) loop
    in ``_promote_top_candidates`` swallows generic
    ``CandidateWriterError`` (missing strategy_configs row, etc.) and
    continues to the next pair. But a ``SchemaNotReadyError`` is an
    INFRASTRUCTURE failure — operator forgot to run migration 020 —
    and must propagate so the CLI exits non-zero. Otherwise a nightly
    cron would log "0 rows inserted" success for every pair while the
    review queue stays empty."""
    import inspect
    from app.strategies import run_multi_strategy_optimizer as rmso
    from app.strategies.candidate_writer import SchemaNotReadyError as _SNRE

    # The module must import the subclass.
    assert _SNRE.__name__ == "SchemaNotReadyError"
    src = inspect.getsource(rmso._promote_top_candidates)
    # The handler must catch SchemaNotReadyError BEFORE the generic
    # CandidateWriterError handler and re-raise (no logging/swallowing).
    assert "except SchemaNotReadyError" in src
    # And the generic CandidateWriterError handler must still exist
    # for per-pair misconfig.
    assert "except CandidateWriterError" in src
    # The SchemaNotReadyError clause must use bare ``raise`` to
    # propagate, not a swallowing ``logger.error(...)`` body.
    snre_idx = src.index("except SchemaNotReadyError")
    cwe_idx = src.index("except CandidateWriterError")
    assert snre_idx < cwe_idx, (
        "SchemaNotReadyError clause must come BEFORE the generic "
        "CandidateWriterError handler so it is matched first"
    )
    body = src[snre_idx:cwe_idx]
    assert "raise" in body
    assert "logger.error" not in body


def test_writer_proceeds_when_schema_probe_returns_table_name():
    """Healthy probe (to_regclass returns the qualified table name) lets
    the writer proceed to its normal supersede + insert path."""
    conn = _FakeConnection()
    # Default fetchone response is the healthy reply; explicitly script
    # it here to make the contract obvious.
    conn.next_fetchone.append(("public.strategy_config_candidates",))
    conn.next_fetch.append([("cfg-1", True)])  # strategy_config_id lookup
    conn.next_rowcount.append(0)
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        top_candidates=[{"params": {"k": 1}, "metrics": {}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    inserted = writer.write_batch(batch, candidates_per_strategy=1)
    assert len(inserted) == 1


# ---------------------------------------------------------------------------
# PR #288 codex round-8 regressions.
# ---------------------------------------------------------------------------


def test_schema_probe_infrastructure_failure_propagates_unwrapped():
    """PR #288 codex round-8 P1: when the ``to_regclass`` probe itself
    fails (DB unreachable, role lacks privileges, connection drops
    mid-query), the ORIGINAL exception must escape ``write_batch``.
    Previously the probe wrapped any exception in
    ``CandidateWriterError``, but the orchestrator's
    ``except CandidateWriterError`` then logged it as a per-pair
    misconfig and continued — turning an infrastructure failure into
    silent "0 rows inserted" success."""

    class _BoomConnection:
        def cursor(self):
            raise OSError("connection reset by peer")

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=lambda *a, **kw: _BoomConnection(),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        top_candidates=[{"params": {"k": 1}, "metrics": {}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    with pytest.raises(Exception) as exc:
        writer.write_batch(batch, candidates_per_strategy=1)
    assert not isinstance(exc.value, CandidateWriterError), (
        "infrastructure probe failures must NOT be wrapped in "
        "CandidateWriterError — the orchestrator swallows that subclass"
    )


def test_candidate_with_falsey_non_mapping_params_raises():
    """PR #288 codex round-8 P2: ``params=[]`` is falsey AND not a
    Mapping. The previous ``candidate.get("params") or {}`` silently
    coerced this to ``{}`` so an empty insert succeeded, hiding the
    malformed optimizer payload from the operator."""
    conn = _FakeConnection()
    conn.next_fetchone.append(("public.strategy_config_candidates",))
    conn.next_fetch.append([("cfg-1", True, None)])
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        top_candidates=[{"params": [], "metrics": {}}],  # falsey non-mapping
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    with pytest.raises(CandidateWriterError, match="params must be a mapping"):
        writer.write_batch(batch, candidates_per_strategy=1)


def test_strategy_configs_lookup_filters_by_underlying_when_tagged():
    """PR #288 codex round-8 P2: multi-underlying tenants may carry
    multiple enabled ``strategy_configs`` rows for the same
    ``strategy_id`` — one per underlying with
    ``params->>'underlying_label'`` set. The lookup must restrict to
    the row whose tag matches the candidate's underlying, not the
    lexicographically first."""
    conn = _FakeConnection()
    conn.next_fetchone.append(("public.strategy_config_candidates",))
    # Two enabled rows: one tagged NIFTY_IDX, one tagged BANKNIFTY_IDX.
    # Without the underlying filter, "cfg-banknifty" (lex < cfg-nifty)
    # would win incorrectly.
    conn.next_fetch.append([
        ("cfg-banknifty", True, "BANKNIFTY_IDX"),
        ("cfg-nifty", True, "NIFTY_IDX"),
    ])
    conn.next_rowcount.append(0)
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        top_candidates=[{"params": {"k": 1}, "metrics": {}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    writer.write_batch(batch, candidates_per_strategy=1)
    insert_executions = [
        (sql, p)
        for sql, p in conn.executed
        if sql.startswith("INSERT INTO public.strategy_config_candidates")
    ]
    assert len(insert_executions) == 1
    _, params = insert_executions[0]
    assert params.get("strategy_config_id") == "cfg-nifty", (
        f"lookup must pick the underlying-tagged row; got "
        f"strategy_config_id={params.get('strategy_config_id')!r}"
    )


def test_strategy_configs_lookup_falls_back_when_no_tagged_row_matches():
    """When no enabled row carries an ``underlying_label`` tag
    matching the candidate, the lookup must fall back to the untagged
    rows so single-underlying tenants (the typical deployment) keep
    working."""
    conn = _FakeConnection()
    conn.next_fetchone.append(("public.strategy_config_candidates",))
    conn.next_fetch.append([("cfg-shared", True, None)])
    conn.next_rowcount.append(0)
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        top_candidates=[{"params": {"k": 1}, "metrics": {}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    writer.write_batch(batch, candidates_per_strategy=1)
    insert_executions = [
        (sql, p)
        for sql, p in conn.executed
        if sql.startswith("INSERT INTO public.strategy_config_candidates")
    ]
    assert len(insert_executions) == 1
    _, params = insert_executions[0]
    assert params.get("strategy_config_id") == "cfg-shared"


# ---------------------------------------------------------------------------
# PR #288 codex round-9 regressions.
# ---------------------------------------------------------------------------


def test_lookup_falls_back_to_untagged_when_no_matching_tag():
    """PR #288 codex round-9 P2: when some enabled rows carry an
    ``underlying_label`` tag for OTHER underlyings AND others are
    untagged, the lookup must prefer the untagged rows over the
    wrong-underlying-tagged ones. Without this, the lex-first match
    could attach a NIFTY_IDX candidate to a BANKNIFTY_IDX-tagged
    row when a generic untagged row also exists."""
    conn = _FakeConnection()
    conn.next_fetchone.append(("public.strategy_config_candidates",))
    # Three enabled rows: BANKNIFTY-tagged (lex-first), NATGAS-tagged,
    # and an untagged generic row.
    conn.next_fetch.append([
        ("cfg-banknifty", True, "BANKNIFTY_IDX"),
        ("cfg-generic", True, None),
        ("cfg-natgas", True, "NG_FUT"),
    ])
    conn.next_rowcount.append(0)
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",  # no row tagged with this
        top_candidates=[{"params": {"k": 1}, "metrics": {}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    writer.write_batch(batch, candidates_per_strategy=1)
    insert_executions = [
        (sql, p) for sql, p in conn.executed
        if sql.startswith("INSERT INTO public.strategy_config_candidates")
    ]
    assert len(insert_executions) == 1
    _, params = insert_executions[0]
    assert params.get("strategy_config_id") == "cfg-generic", (
        f"lookup must prefer the untagged generic row over the "
        f"wrong-underlying-tagged ones; got "
        f"strategy_config_id={params.get('strategy_config_id')!r}"
    )


def test_lookup_raises_when_all_enabled_rows_are_wrong_underlying():
    """When ALL enabled rows are tagged with underlyings that do NOT
    match the candidate's, and no untagged row exists, the lookup
    must FAIL LOUDLY rather than silently attaching the candidate to
    the wrong underlying's config row."""
    conn = _FakeConnection()
    conn.next_fetchone.append(("public.strategy_config_candidates",))
    conn.next_fetch.append([
        ("cfg-banknifty", True, "BANKNIFTY_IDX"),
        ("cfg-natgas", True, "NG_FUT"),
    ])
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        top_candidates=[{"params": {"k": 1}, "metrics": {}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    with pytest.raises(CandidateWriterError, match="no strategy_configs row matches"):
        writer.write_batch(batch, candidates_per_strategy=1)
    # No INSERT should have been attempted.
    assert not any(
        sql.startswith("INSERT INTO public.strategy_config_candidates")
        for sql, _ in conn.executed
    )


# ---------------------------------------------------------------------------
# PR #288 codex round-10 regressions.
# ---------------------------------------------------------------------------


def test_lookup_prefers_disabled_matching_underlying_over_enabled_other(caplog):
    """PR #288 codex round-10 P2: when a tenant has a DISABLED row
    tagged with the candidate's underlying AND ENABLED rows tagged
    with OTHER underlyings, the lookup must pick the disabled matching
    row (with a warning) rather than raise. Silent attachment to the
    wrong underlying's enabled row would mutate live trading on
    approval; the disabled-matching path at least lets the operator
    re-enable the right config later."""
    conn = _FakeConnection()
    conn.next_fetchone.append(("public.strategy_config_candidates",))
    # cfg-banknifty is ENABLED but tagged BANKNIFTY (wrong underlying).
    # cfg-nifty is DISABLED but tagged NIFTY (right underlying).
    conn.next_fetch.append([
        ("cfg-banknifty", True, "BANKNIFTY_IDX"),
        ("cfg-nifty", False, "NIFTY_IDX"),
    ])
    conn.next_rowcount.append(0)
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        top_candidates=[{"params": {"k": 1}, "metrics": {}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    with caplog.at_level("WARNING"):
        writer.write_batch(batch, candidates_per_strategy=1)
    insert_executions = [
        (sql, p) for sql, p in conn.executed
        if sql.startswith("INSERT INTO public.strategy_config_candidates")
    ]
    assert len(insert_executions) == 1
    _, params = insert_executions[0]
    assert params.get("strategy_config_id") == "cfg-nifty", (
        "must prefer the disabled matching-underlying row over the "
        "enabled wrong-underlying row"
    )
    assert any("disabled" in r.message.lower() for r in caplog.records), (
        "must warn loudly when attaching to a disabled config"
    )


def test_lookup_raises_when_only_wrong_underlying_disabled_rows_exist():
    """When ALL rows are disabled AND tagged with non-matching
    underlyings, the lookup must raise rather than fall back to the
    lex-first wrong-underlying disabled row."""
    conn = _FakeConnection()
    conn.next_fetchone.append(("public.strategy_config_candidates",))
    conn.next_fetch.append([
        ("cfg-banknifty", False, "BANKNIFTY_IDX"),
        ("cfg-natgas", False, "NG_FUT"),
    ])
    writer = CandidateWriter(
        tenant_id="t",
        broker_account_id="a",
        optimizer_version="v1",
        connect_fn=_connect_fn_returning(conn),
    )
    batch = CandidateBatch(
        strategy_name="ema20",
        underlying_label="NIFTY_IDX",
        top_candidates=[{"params": {"k": 1}, "metrics": {}}],
        backtest_window=(date(2026, 5, 1), date(2026, 5, 14)),
    )
    with pytest.raises(CandidateWriterError, match="no strategy_configs row matches"):
        writer.write_batch(batch, candidates_per_strategy=1)
    # No INSERT should have been attempted.
    assert not any(
        sql.startswith("INSERT INTO public.strategy_config_candidates")
        for sql, _ in conn.executed
    )
