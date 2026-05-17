"""Training-row builder for the OI/ML intraday option-sell model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from app.data.option_chain_provider import (
    OptionQuote,
    is_quote_usable_for_live_entry,
)
from app.features.oi_features import build_oi_features
from app.strategies.oi_ml.labels import (
    IntradayLabelConfig,
    IntradayShortOptionLabel,
    label_candidate_from_repository,
)


@dataclass(frozen=True)
class OiMlDatasetConfig:
    option_type: str = "CE"
    max_snapshot_age_seconds: int = 120
    min_premium: float = 1.0
    min_oi: int = 1
    min_otm_points: float = 0.0
    max_otm_points: float | None = None
    max_candidates_per_decision: int | None = None
    require_underlying_ltp: bool = True
    require_live_usable_quote: bool = True
    skip_unlabelable: bool = True
    wall_multiple: float = 2.0
    label_config: IntradayLabelConfig = field(default_factory=IntradayLabelConfig)


@dataclass(frozen=True)
class OiMlTrainingRow:
    decision_ts: datetime
    snapshot_ts: datetime
    underlying: str
    expiry: date
    strike: int
    option_type: str
    provider: str
    features: Mapping[str, Any]
    label: IntradayShortOptionLabel
    candidate_quality_flags: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "decision_ts": self.decision_ts.isoformat(),
            "snapshot_ts": self.snapshot_ts.isoformat(),
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "option_type": self.option_type,
            "provider": self.provider,
            "entry_ts": self.label.entry_ts.isoformat(),
            "exit_ts": self.label.exit_ts.isoformat(),
            "entry_premium": self.label.entry_premium,
            "exit_premium": self.label.exit_premium,
            "exit_reason": self.label.exit_reason.value,
            "primary_label": self.label.primary_label,
            "profitable_label": self.label.profitable_label,
            "pnl_per_lot": self.label.pnl_per_lot,
            "mae_premium": self.label.mae_premium,
            "mae_multiple": self.label.mae_multiple,
            "mfe_premium": self.label.mfe_premium,
            "bars_seen": self.label.bars_seen,
            "candidate_quality_flags": list(self.candidate_quality_flags),
            "label_quality_flags": list(self.label.quality_flags),
        }
        for key, value in self.features.items():
            row[f"feature_{key}"] = value
        return row


class OiMlDatasetBuilder:
    """Build flat ML rows from option-chain snapshots and label windows."""

    def __init__(
        self,
        repository: Any,
        *,
        config: OiMlDatasetConfig | None = None,
    ) -> None:
        self.repository = repository
        self.config = config or OiMlDatasetConfig()

    def build_rows_for_decision(
        self,
        *,
        underlying: str,
        expiry: date,
        decision_ts: datetime,
        provider: str | None = None,
    ) -> list[OiMlTrainingRow]:
        decision = _aware_utc(decision_ts)
        min_snapshot_ts = decision - timedelta(
            seconds=max(0, int(self.config.max_snapshot_age_seconds))
        )
        snapshot = self.repository.fetch_latest_snapshot(
            underlying=underlying,
            expiry=expiry,
            decision_ts=decision,
            min_snapshot_ts=min_snapshot_ts,
            provider=provider,
        )
        if not snapshot:
            return []

        candidates = select_candidate_quotes(
            snapshot,
            decision_ts=decision,
            config=self.config,
        )
        rows: list[OiMlTrainingRow] = []
        for candidate in candidates:
            try:
                features = build_oi_features(
                    snapshot,
                    candidate_strike=candidate.strike,
                    option_type=candidate.option_type,
                    decision_ts=decision,
                    underlying_ltp=_spot_from_snapshot(snapshot),
                    wall_multiple=self.config.wall_multiple,
                )
                label = label_candidate_from_repository(
                    self.repository,
                    underlying=underlying,
                    expiry=expiry,
                    strike=candidate.strike,
                    option_type=candidate.option_type,
                    decision_ts=decision,
                    provider=provider,
                    config=self.config.label_config,
                )
            except ValueError:
                if self.config.skip_unlabelable:
                    continue
                raise
            rows.append(
                OiMlTrainingRow(
                    decision_ts=decision,
                    snapshot_ts=candidate.snapshot_ts,
                    underlying=candidate.underlying,
                    expiry=candidate.expiry,
                    strike=candidate.strike,
                    option_type=candidate.option_type,
                    provider=candidate.provider,
                    features=features,
                    label=label,
                    candidate_quality_flags=tuple(sorted((candidate.quality_flags or {}).keys())),
                )
            )
        return rows

    def build_rows_for_decisions(
        self,
        *,
        underlying: str,
        expiry: date,
        decision_times: Iterable[datetime],
        provider: str | None = None,
    ) -> list[OiMlTrainingRow]:
        rows: list[OiMlTrainingRow] = []
        for decision_ts in decision_times:
            rows.extend(
                self.build_rows_for_decision(
                    underlying=underlying,
                    expiry=expiry,
                    decision_ts=decision_ts,
                    provider=provider,
                )
            )
        return rows


def select_candidate_quotes(
    snapshot_quotes: Sequence[OptionQuote],
    *,
    decision_ts: datetime,
    config: OiMlDatasetConfig | None = None,
) -> list[OptionQuote]:
    cfg = config or OiMlDatasetConfig()
    side = str(cfg.option_type or "CE").strip().upper()
    decision = _aware_utc(decision_ts)
    rows = [quote.normalized() for quote in snapshot_quotes]
    if any(row.snapshot_ts > decision for row in rows):
        raise ValueError("candidate snapshot contains future quotes")

    spot = _spot_from_snapshot(rows)
    if spot is None and cfg.require_underlying_ltp:
        return []

    candidates: list[OptionQuote] = []
    for row in rows:
        if row.option_type != side:
            continue
        if cfg.require_live_usable_quote and not is_quote_usable_for_live_entry(row):
            continue
        if int(row.oi or 0) < int(cfg.min_oi):
            continue
        premium = _trigger_premium(row)
        if premium is None or premium < float(cfg.min_premium):
            continue
        if spot is not None and not _passes_otm_filter(row, spot, cfg):
            continue
        candidates.append(row)

    candidates.sort(key=lambda row: (_distance_from_spot(row, spot), row.strike))
    if cfg.max_candidates_per_decision is not None:
        return candidates[: max(0, int(cfg.max_candidates_per_decision))]
    return candidates


def _passes_otm_filter(row: OptionQuote, spot: float, cfg: OiMlDatasetConfig) -> bool:
    distance = float(row.strike) - float(spot)
    side = row.option_type
    if side == "PE":
        distance = float(spot) - float(row.strike)
    if distance < float(cfg.min_otm_points):
        return False
    if cfg.max_otm_points is not None and distance > float(cfg.max_otm_points):
        return False
    return True


def _distance_from_spot(row: OptionQuote, spot: float | None) -> float:
    if spot is None:
        return float(row.strike)
    return abs(float(row.strike) - float(spot))


def _spot_from_snapshot(rows: Sequence[OptionQuote]) -> float | None:
    for row in rows:
        parsed = _float(row.underlying_ltp)
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _trigger_premium(row: OptionQuote) -> float | None:
    return _first_positive(row.ltp, _mid(row), row.ask, row.bid)


def _mid(row: OptionQuote) -> float | None:
    bid = _float(row.bid)
    ask = _float(row.ask)
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def _first_positive(*values: object) -> float | None:
    for value in values:
        parsed = _float(value)
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _float(value: object) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "OiMlDatasetBuilder",
    "OiMlDatasetConfig",
    "OiMlTrainingRow",
    "select_candidate_quotes",
]
