"""Persist optimizer-proposed parameter sets into strategy_config_candidates.

Issue #272 (epic #270). The nightly parameter optimizer never writes to
``strategy_configs`` directly — it inserts ``status='pending'`` rows here,
which an operator reviews and approves via the admin API (#275). Approval
is the only path that mutates a live trading config.

Schema dependency
-----------------
This writer requires ``public.strategy_config_candidates`` to exist on
the target Postgres. The schema is created by
``migrations/020_strategy_config_candidates.sql`` — that migration
lives in PR #281 (also part of epic #270), and the deploy order for
the epic is:

  1. PR #281 — migration 020 creates ``strategy_config_candidates``
  2. PR #283 — optimizer framework lands the simulators
  3. PR #288 (this PR's writer) — INSERTs into the table from (1)

A repo-wide search for ``strategy_config_candidates`` in *this* PR
only finds the writer and tests, which has confused codex multiple
times — the migration is intentionally shipped as a separate slice
(schema vs writer) to keep PRs reviewable. The deploy-time guard
``CandidateWriter._assert_schema_ready`` below raises an actionable
error if the table is missing, so a fresh database without migration
020 fails fast with a clear message instead of an opaque
``relation ... does not exist``.

Contract:
- One transaction per (strategy_config_id, params) pair so a mid-batch
  failure cannot leave the queue partially updated.
- Idempotent re-runs: existing ``status='pending'`` rows with the same
  ``params`` JSONB (Postgres-canonical equality) are flipped to
  ``status='superseded'`` before the new row is inserted. Re-running the
  same optimizer twice in a row therefore produces one ``pending`` row
  per candidate plus a history of ``superseded`` rows for audit.
- ``backtest_window`` is the actual indicator_bars date range pulled by
  the optimizer (not synthetic), so the metrics on each candidate are
  reproducible from the recorded ``optimizer_version`` + window.
- ``optimizer_version`` is the image's git SHA (env ``IMAGE_TAG`` if
  set, otherwise ``git rev-parse HEAD``, otherwise the literal string
  ``"unknown"`` so the writer cannot block on environment issues).

The writer is a separate module (not inlined into the runner) so the
admin API and a future Notebook-style ad-hoc tool can both use the same
insert path with the same supersede semantics.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# Maps the optimizer's internal short strategy name (used as a key in
# ``MultiStrategyOptimizer.results``) to the canonical ``strategy_id``
# stored in ``strategy_configs.strategy_id``. The canonical names are
# the ones migration 002 harmonized to (snake_case).
STRATEGY_NAME_TO_CONFIG_STRATEGY_ID: Mapping[str, str] = {
    "ema20": "ema20_strategy",
    "exclusive_nifty_ce": "exclusive_nifty_ce_buy",
    "put_momentum": "put_momentum_scalper",
}


@dataclass(frozen=True)
class CandidateBatch:
    """A single (strategy, underlying) result ready to be persisted."""

    strategy_name: str
    underlying_label: str
    top_candidates: Sequence[Mapping[str, Any]]  # each {"params": dict, "metrics": dict}
    backtest_window: Tuple[date, date]


class CandidateWriterError(RuntimeError):
    """Raised when the writer cannot persist candidates (resolution / SQL)."""


class SchemaNotReadyError(CandidateWriterError):
    """Raised when migration 020 has not been applied to the target DB.

    PR #288 codex round-7 P1: a missing ``strategy_config_candidates``
    table is an INFRASTRUCTURE failure (operator didn't run the
    migration), not a per-(strategy, underlying) misconfig. The
    promotion orchestrator must let this propagate instead of looping
    over every pair and logging "0 rows inserted" success.
    """


def _normalize_for_json(value: Any) -> Any:
    """Convert NumPy / pandas scalars to native Python types so
    ``json.dumps`` writes real JSON booleans / numbers instead of
    falling back to ``str(value)``.

    PR #288 codex round-5 P2: optimizer parameter spaces produce
    NumPy values (``np.bool_`` for booleans, ``np.float64`` for
    floats, ``np.int64`` for ints). Without normalization the
    candidate writer stored ``"False"`` instead of ``false`` in the
    ``params`` JSONB column, so the supersede match
    (``WHERE params = $1::jsonb``) on the next run never matched the
    new run's normalized JSON and left duplicate pending rows behind.

    Recursive so nested dicts (e.g. ``metrics["walk_forward"]`` from
    #289) are normalized too.
    """
    if isinstance(value, dict):
        return {k: _normalize_for_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_for_json(v) for v in value]
    # NumPy scalars expose ``.item()`` that returns the native type.
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return value.item()
        except (AttributeError, ValueError):
            pass
    # Booleans, ints, floats, strings, None — already JSON-native.
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    # Anything else (dates, etc.): str fallback so we never raise.
    return str(value)


def _resolve_optimizer_version() -> str:
    """Git SHA of the optimizer image. Falls back to ``"unknown"`` rather
    than raising so a missing build-time tag cannot block a nightly run.
    """
    image_tag = os.getenv("IMAGE_TAG", "").strip()
    if image_tag:
        return image_tag
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode("ascii").strip()
        return sha or "unknown"
    except Exception:
        return "unknown"


class CandidateWriter:
    """Resolves strategy_config_id and persists candidate rows."""

    def __init__(
        self,
        *,
        tenant_id: str,
        broker_account_id: str,
        dsn: Optional[str] = None,
        optimizer_version: Optional[str] = None,
        connect_fn: Optional[Callable[..., Any]] = None,
        strategy_id_overrides: Optional[Mapping[str, str]] = None,
    ) -> None:
        """
        Args:
            tenant_id: tenant scoping the strategy_configs lookup.
            broker_account_id: broker account scoping the lookup.
            dsn: explicit Postgres DSN; if ``None``, resolved from settings
                via ``app.data.postgres.get_control_plane_dsn``.
            optimizer_version: override the auto-resolved git SHA. Set
                from CLI flags or env in tests.
            connect_fn: injection point for tests — must return a context
                manager yielding a psycopg-like connection. Production
                uses ``app.data.postgres.connect_with_retry``.
            strategy_id_overrides: per-strategy_name override of the
                canonical strategy_id mapping (rarely used; for
                experimental strategies that don't follow the
                ``<short>_<suffix>`` convention).
        """
        if not tenant_id:
            raise CandidateWriterError("tenant_id is required")
        if not broker_account_id:
            raise CandidateWriterError("broker_account_id is required")
        self._tenant_id = tenant_id
        self._broker_account_id = broker_account_id
        self._dsn = dsn
        self._optimizer_version = optimizer_version or _resolve_optimizer_version()
        self._connect_fn = connect_fn
        self._strategy_id_map: Dict[str, str] = dict(STRATEGY_NAME_TO_CONFIG_STRATEGY_ID)
        if strategy_id_overrides:
            self._strategy_id_map.update(strategy_id_overrides)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def optimizer_version(self) -> str:
        return self._optimizer_version

    def write_batch(
        self,
        batch: CandidateBatch,
        candidates_per_strategy: int = 3,
    ) -> List[str]:
        """Insert up to ``candidates_per_strategy`` rows for one batch.

        Returns the list of newly inserted candidate_ids. May be shorter
        than ``candidates_per_strategy`` if the batch has fewer
        candidates.
        """
        if candidates_per_strategy <= 0:
            return []

        strategy_id = self._resolve_strategy_id(batch.strategy_name)
        to_write = list(batch.top_candidates[:candidates_per_strategy])
        if not to_write:
            logger.warning(
                "candidate_writer: empty top_candidates for strategy=%s underlying=%s — skipping",
                batch.strategy_name,
                batch.underlying_label,
            )
            return []

        inserted: List[str] = []
        with self._connection() as conn:
            # PR #288 codex round-6 P1: surface a clean, actionable
            # error if migration 020 (from PR #281) hasn't been applied
            # yet. Without this guard the writer's first INSERT raises
            # a Postgres ``relation "strategy_config_candidates" does
            # not exist`` deep inside psycopg, which is harder to
            # connect back to "run the migration step".
            self._assert_schema_ready(conn)
            strategy_config_id = self._lookup_strategy_config_id(
                conn,
                strategy_id,
                underlying_label=batch.underlying_label,
            )
            for candidate in to_write:
                # PR #288 codex round-8 P2: previously
                # ``candidate.get("params") or {}`` would silently
                # coerce a falsey non-mapping payload (e.g.
                # ``params=[]``, ``params=0``) to ``{}``, letting an
                # empty candidate insert succeed instead of surfacing
                # the malformed optimizer/ad-hoc payload. Now use
                # ``is None`` so genuinely missing keys still default
                # to an empty mapping while non-mapping types fall
                # through to the Mapping-type check below.
                params_raw = candidate.get("params")
                metrics_raw = candidate.get("metrics")
                params = {} if params_raw is None else params_raw
                metrics = {} if metrics_raw is None else metrics_raw
                if not isinstance(params, Mapping):
                    raise CandidateWriterError(
                        f"candidate.params must be a mapping, got {type(params).__name__}"
                    )
                if not isinstance(metrics, Mapping):
                    raise CandidateWriterError(
                        f"candidate.metrics must be a mapping, got {type(metrics).__name__}"
                    )
                # PR #288 codex round-3 P2: stamp the underlying onto the
                # metrics JSONB so reviewers can tell which underlying a
                # candidate was scored on, and so identical params on
                # different underlyings supersede each other only when
                # they share the underlying. The schema has no dedicated
                # ``underlying_label`` column; storing it in metrics
                # keeps the migration footprint zero while still letting
                # the supersede WHERE clause discriminate via JSONB
                # extraction.
                #
                # PR #288 codex round-4 P2: OVERWRITE any caller-supplied
                # ``underlying_label`` unconditionally. ``setdefault``
                # used to preserve a stale value (e.g. when an ad-hoc
                # tool reused a candidate payload from a previous run),
                # making the supersede match against ``batch.underlying_label``
                # miss and leaving duplicate pending rows behind.
                metrics_with_underlying = dict(metrics)
                metrics_with_underlying["underlying_label"] = batch.underlying_label
                candidate_id = self._insert_candidate(
                    conn=conn,
                    strategy_config_id=strategy_config_id,
                    params=dict(params),
                    metrics=metrics_with_underlying,
                    backtest_window=batch.backtest_window,
                    underlying_label=batch.underlying_label,
                )
                inserted.append(candidate_id)

        logger.info(
            "candidate_writer: wrote %d/%d for strategy=%s underlying=%s "
            "(strategy_config_id=%s, optimizer_version=%s)",
            len(inserted),
            candidates_per_strategy,
            batch.strategy_name,
            batch.underlying_label,
            strategy_config_id,
            self._optimizer_version,
        )
        return inserted

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_strategy_id(self, strategy_name: str) -> str:
        try:
            return self._strategy_id_map[strategy_name]
        except KeyError as exc:
            raise CandidateWriterError(
                f"unknown strategy_name {strategy_name!r}; known: "
                f"{sorted(self._strategy_id_map)}"
            ) from exc

    def _connection(self):
        if self._connect_fn is not None:
            return self._connect_fn()
        # Late imports so module remains importable without psycopg.
        from app.data.postgres import connect_with_retry, get_control_plane_dsn

        dsn = self._dsn or get_control_plane_dsn()
        return connect_with_retry(dsn, autocommit=False)

    def _assert_schema_ready(self, conn: Any) -> None:
        """Raise a clean ``CandidateWriterError`` if migration 020 has
        not been applied yet (PR #288 codex round-6 P1).

        Uses Postgres' ``to_regclass`` which returns ``NULL`` when the
        named relation doesn't exist — no permission errors, no
        exception path. Lets the orchestrator surface a one-line
        actionable message:

            CandidateWriterError: public.strategy_config_candidates
            is missing. Apply migration
            ``migrations/020_strategy_config_candidates.sql`` (from
            PR #281) before --promote-to-candidate.
        """
        # PR #288 codex round-8 P1: if the probe itself fails (DB
        # unreachable, role can't execute ``to_regclass``, connection
        # drops mid-query), let the ORIGINAL exception propagate.
        # Previously we wrapped it in ``CandidateWriterError``, but the
        # orchestrator's ``except CandidateWriterError`` handler then
        # logged it as a per-pair misconfig and continued. An
        # infrastructure failure must surface as a non-zero exit, the
        # same fail-fast behaviour the round-4 P2 spec requires for
        # generic loader/SQL errors.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.strategy_config_candidates')"
            )
            row = cur.fetchone()
        if not row or row[0] is None:
            # PR #288 codex round-7 P1: raise the SchemaNotReadyError
            # subclass so the orchestrator can let it escape instead of
            # absorbing it into the per-(strategy, underlying) handler.
            raise SchemaNotReadyError(
                "public.strategy_config_candidates is missing. Apply "
                "migration migrations/020_strategy_config_candidates.sql "
                "(from PR #281, epic #270) before --promote-to-candidate."
            )

    def _lookup_strategy_config_id(
        self,
        conn: Any,
        strategy_id: str,
        *,
        underlying_label: Optional[str] = None,
    ) -> str:
        """Resolve strategy_config_id for the configured tenant+account+strategy.

        PR #288 codex round-3 P2: prefers ``enabled = TRUE`` rows over
        disabled/stale duplicates. Without this, a tenant with both an
        enabled and a disabled strategy_configs row for the same
        ``(tenant, broker_account, strategy_id)`` tuple could have its
        candidate attached to the disabled row — approving it would
        mutate a config the hub-routing layer skips while the actual
        live route remained untouched.

        PR #288 codex round-8 P2: when ``underlying_label`` is supplied
        AND any row's ``params->>'underlying_label'`` matches, the
        match is restricted to that subset. This prevents an EMA20
        candidate scored on ``NIFTY_IDX`` from being attached to a
        ``BANKNIFTY_IDX``-tagged strategy_configs row in
        multi-underlying tenants. Rows without an
        ``underlying_label`` in params (legacy / single-underlying
        tenants) are still matched when no per-underlying row exists,
        so existing deployments aren't broken.

        Order:
          1. enabled per-underlying rows ordered by ``strategy_config_id``
          2. enabled untagged rows ordered by ``strategy_config_id``
          3. disabled rows ordered by ``strategy_config_id``

        Raises ``CandidateWriterError`` if no row matches. If multiple
        ENABLED rows match (which shouldn't happen but is not enforced
        at the schema level) the lexicographically first is used and a
        warning is logged so the operator can deduplicate the registry.
        """
        # ORDER BY ``enabled DESC`` puts TRUE before FALSE (Postgres
        # boolean ordering); within each group sort by strategy_config_id
        # for determinism. Also project ``params->>'underlying_label'``
        # so the Python layer can filter for per-underlying matches.
        sql = (
            "SELECT strategy_config_id, enabled, "
            "       params->>'underlying_label' AS cfg_underlying "
            "FROM public.strategy_configs "
            "WHERE tenant_id = %(tenant_id)s "
            "  AND broker_account_id = %(broker_account_id)s "
            "  AND strategy_id = %(strategy_id)s "
            "ORDER BY enabled DESC, strategy_config_id"
        )
        params = {
            "tenant_id": self._tenant_id,
            "broker_account_id": self._broker_account_id,
            "strategy_id": strategy_id,
        }
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        if not rows:
            raise CandidateWriterError(
                f"no strategy_configs row for tenant={self._tenant_id!r} "
                f"broker_account={self._broker_account_id!r} "
                f"strategy_id={strategy_id!r}"
            )

        # Defensive: existing fixtures and the round-3 schema may
        # return 2-tuples ``(id, enabled)`` without the projected
        # ``cfg_underlying`` column. Treat a missing third element
        # as untagged.
        def _row_underlying(r):
            return r[2] if len(r) > 2 else None

        enabled_rows = [r for r in rows if r[1]]
        disabled_rows = [r for r in rows if not r[1]]

        # PR #288 codex round-8/9/10 P2: priority order for picking
        # the strategy_config_id when ``underlying_label`` is known:
        #   1. enabled rows tagged with the candidate's underlying
        #   2. enabled rows with no underlying tag (legacy / generic)
        #   3. disabled rows tagged with the candidate's underlying
        #      (warn loudly — operator must re-enable to promote)
        #   4. disabled untagged rows (legacy generic, warn loudly)
        #   5. otherwise raise — only wrong-underlying rows exist
        #
        # When ``underlying_label`` is None (round-3 callers, untagged
        # tenants) the lookup is enabled-first then disabled-first
        # across the full set, preserving prior behaviour.
        if underlying_label:
            matching_enabled = [
                r for r in enabled_rows if _row_underlying(r) == underlying_label
            ]
            untagged_enabled = [
                r for r in enabled_rows if _row_underlying(r) is None
            ]
            matching_disabled = [
                r for r in disabled_rows if _row_underlying(r) == underlying_label
            ]
            untagged_disabled = [
                r for r in disabled_rows if _row_underlying(r) is None
            ]
            other_enabled = [
                r for r in enabled_rows
                if _row_underlying(r) not in (None, underlying_label)
            ]
            other_disabled = [
                r for r in disabled_rows
                if _row_underlying(r) not in (None, underlying_label)
            ]

            chosen_row = None
            chosen_set: List = []
            used_disabled = False
            if matching_enabled:
                chosen_set = matching_enabled
            elif untagged_enabled:
                chosen_set = untagged_enabled
            elif matching_disabled:
                chosen_set = matching_disabled
                used_disabled = True
            elif untagged_disabled:
                chosen_set = untagged_disabled
                used_disabled = True
            if chosen_set:
                chosen_row = chosen_set[0]
                if len(chosen_set) > 1:
                    logger.warning(
                        "candidate_writer: %d strategy_configs rows match "
                        "(tenant=%s, broker=%s, strategy_id=%s, "
                        "underlying=%s); using first (strategy_config_id=%s). "
                        "Deduplicate the registry.",
                        len(chosen_set),
                        self._tenant_id,
                        self._broker_account_id,
                        strategy_id,
                        underlying_label,
                        chosen_row[0],
                    )
            else:
                # PR #288 codex round-10 P2: only wrong-underlying
                # rows exist (enabled or disabled). Silent attachment
                # to the wrong underlying's config row would mutate
                # live trading behaviour on approval.
                other_tags = sorted({
                    _row_underlying(r)
                    for r in (other_enabled + other_disabled)
                    if _row_underlying(r)
                })
                raise CandidateWriterError(
                    f"no strategy_configs row matches "
                    f"underlying={underlying_label!r}; tenant has "
                    f"per-underlying rows tagged {other_tags} but none "
                    f"for this candidate. Add a strategy_configs row "
                    "for the candidate's underlying or set "
                    "params->>'underlying_label' to NULL on a generic row."
                )
            assert chosen_row is not None
            chosen = chosen_row[0]
            if used_disabled:
                logger.warning(
                    "candidate_writer: only disabled strategy_configs rows "
                    "match (tenant=%s, broker=%s, strategy_id=%s, "
                    "underlying=%s); attaching candidate to disabled "
                    "strategy_config_id=%s. Re-enable the config "
                    "before approval or the promotion will have no "
                    "live effect.",
                    self._tenant_id,
                    self._broker_account_id,
                    strategy_id,
                    underlying_label,
                    chosen,
                )
            return chosen

        # No ``underlying_label`` — preserve the round-3 contract.
        if enabled_rows:
            chosen = enabled_rows[0][0]
            if len(enabled_rows) > 1:
                logger.warning(
                    "candidate_writer: %d ENABLED strategy_configs rows match "
                    "(tenant=%s, broker=%s, strategy_id=%s); using first "
                    "(strategy_config_id=%s). Deduplicate the registry.",
                    len(enabled_rows),
                    self._tenant_id,
                    self._broker_account_id,
                    strategy_id,
                    chosen,
                )
            return chosen
        chosen = rows[0][0]
        logger.warning(
            "candidate_writer: only disabled strategy_configs rows match "
            "(tenant=%s, broker=%s, strategy_id=%s); attaching candidate "
            "to disabled strategy_config_id=%s. Re-enable the config "
            "before approval or the promotion will have no live effect.",
            self._tenant_id,
            self._broker_account_id,
            strategy_id,
            chosen,
        )
        return chosen

    def _insert_candidate(
        self,
        *,
        conn: Any,
        strategy_config_id: str,
        params: Dict[str, Any],
        metrics: Dict[str, Any],
        backtest_window: Tuple[date, date],
        underlying_label: str,
    ) -> str:
        """Supersede prior pending rows with identical params, then insert.

        Single transaction per candidate so a duplicate-supersede +
        partial-insert sequence cannot leave the queue inconsistent.

        PR #288 codex round-3 P2: the supersede match now includes the
        ``underlying_label`` stored in ``metrics->>'underlying_label'``
        so two underlyings producing the same parameter set under one
        ``strategy_config_id`` (a real case for multi-underlying
        strategies like EMA20 / PM) do not clobber each other's pending
        rows.
        """
        start_d, end_d = backtest_window
        if not isinstance(start_d, date) or not isinstance(end_d, date):
            raise CandidateWriterError(
                "backtest_window must be a (date, date) tuple"
            )
        if end_d < start_d:
            raise CandidateWriterError(
                f"backtest_window end ({end_d}) precedes start ({start_d})"
            )

        # PR #288 codex round-5 P2: normalize NumPy scalars (np.bool_,
        # np.float64, np.int64) to native Python types BEFORE serialization
        # so the JSONB column stores real JSON booleans / numbers instead
        # of stringified ``"False"`` / ``"123.4"``. The previous
        # ``default=str`` fallback ran AFTER json.dumps tried the standard
        # encoder, which knows how to encode np scalars only if they are
        # explicitly cast — for ``np.bool_`` and ``np.integer`` it raised
        # ``TypeError`` and fell through to ``str()``, corrupting the
        # supersede match.
        normalized_params = _normalize_for_json(params)
        normalized_metrics = _normalize_for_json(metrics)
        params_json = json.dumps(normalized_params, sort_keys=True)
        metrics_json = json.dumps(normalized_metrics, sort_keys=True)
        candidate_id = uuid.uuid4().hex
        # Postgres ``daterange`` literal: inclusive lower, inclusive upper
        # using ``[]`` so a single-day window (start == end) still covers
        # that day.
        window_literal = f"[{start_d.isoformat()},{end_d.isoformat()}]"

        supersede_sql = (
            "UPDATE public.strategy_config_candidates "
            "SET status = 'superseded', reviewed_at = NOW(), reviewed_by = %(actor)s "
            "WHERE strategy_config_id = %(strategy_config_id)s "
            "  AND status = 'pending' "
            "  AND params = %(params)s::jsonb "
            "  AND metrics->>'underlying_label' = %(underlying_label)s"
        )
        insert_sql = (
            "INSERT INTO public.strategy_config_candidates ("
            "candidate_id, strategy_config_id, params, metrics, "
            "backtest_window, optimizer_version, status"
            ") VALUES ("
            "%(candidate_id)s, %(strategy_config_id)s, "
            "%(params)s::jsonb, %(metrics)s::jsonb, "
            "%(backtest_window)s::daterange, %(optimizer_version)s, 'pending'"
            ")"
        )
        actor = f"optimizer:{self._optimizer_version}"
        supersede_args = {
            "strategy_config_id": strategy_config_id,
            "params": params_json,
            "actor": actor,
            "underlying_label": underlying_label,
        }
        insert_args = {
            "candidate_id": candidate_id,
            "strategy_config_id": strategy_config_id,
            "params": params_json,
            "metrics": metrics_json,
            "backtest_window": window_literal,
            "optimizer_version": self._optimizer_version,
        }
        try:
            with conn.cursor() as cur:
                cur.execute(supersede_sql, supersede_args)
                superseded = cur.rowcount or 0
                cur.execute(insert_sql, insert_args)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                logger.exception("candidate_writer: rollback failed")
            raise
        if superseded:
            logger.info(
                "candidate_writer: superseded %d prior pending row(s) "
                "before inserting candidate_id=%s",
                superseded,
                candidate_id,
            )
        return candidate_id
