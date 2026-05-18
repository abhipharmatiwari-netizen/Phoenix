"""Cross-provider option-chain validation helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from app.data.option_chain_provider import OptionQuote


@dataclass(frozen=True)
class OptionChainValidationConfig:
    oi_abs_tolerance: int = 0
    volume_abs_tolerance: int = 250
    volume_pct_tolerance: float = 0.05
    price_abs_tolerance: float = 0.10
    price_pct_tolerance: float = 0.01
    iv_abs_tolerance: float = 0.50
    iv_pct_tolerance: float = 0.05


@dataclass(frozen=True)
class OptionChainFieldDiff:
    field: str
    angel_value: str | None
    nse_value: str | None
    abs_diff: str | None = None
    pct_diff: str | None = None
    tolerance: str | None = None


@dataclass(frozen=True)
class OptionChainContractDiff:
    strike: int
    option_type: str
    field_diffs: tuple[OptionChainFieldDiff, ...] = ()


@dataclass(frozen=True)
class OptionChainValidationReport:
    underlying: str
    expiry: str
    compared_contracts: int
    angel_only_contracts: tuple[tuple[int, str], ...] = ()
    nse_only_contracts: tuple[tuple[int, str], ...] = ()
    mismatches: tuple[OptionChainContractDiff, ...] = ()
    missing_angel_iv: int = 0
    missing_nse_iv: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.angel_only_contracts and not self.nse_only_contracts and not self.mismatches

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "expiry": self.expiry,
            "ok": self.ok,
            "compared_contracts": self.compared_contracts,
            "angel_only_contracts": [
                {"strike": strike, "option_type": option_type}
                for strike, option_type in self.angel_only_contracts
            ],
            "nse_only_contracts": [
                {"strike": strike, "option_type": option_type}
                for strike, option_type in self.nse_only_contracts
            ],
            "mismatches": [
                {
                    "strike": mismatch.strike,
                    "option_type": mismatch.option_type,
                    "field_diffs": [
                        {
                            "field": diff.field,
                            "angel_value": diff.angel_value,
                            "nse_value": diff.nse_value,
                            "abs_diff": diff.abs_diff,
                            "pct_diff": diff.pct_diff,
                            "tolerance": diff.tolerance,
                        }
                        for diff in mismatch.field_diffs
                    ],
                }
                for mismatch in self.mismatches
            ],
            "missing_angel_iv": self.missing_angel_iv,
            "missing_nse_iv": self.missing_nse_iv,
            "metadata": dict(self.metadata or {}),
        }


def compare_angel_to_nse(
    angel_quotes: Sequence[OptionQuote],
    nse_quotes: Sequence[OptionQuote],
    *,
    config: OptionChainValidationConfig | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> OptionChainValidationReport:
    cfg = config or OptionChainValidationConfig()
    angel = {_key(quote): quote.normalized() for quote in angel_quotes}
    nse = {_key(quote): quote.normalized() for quote in nse_quotes}
    angel_keys = set(angel)
    nse_keys = set(nse)
    common_keys = sorted(angel_keys & nse_keys)

    mismatches: list[OptionChainContractDiff] = []
    for key in common_keys:
        diffs = _compare_quote_fields(angel[key], nse[key], cfg)
        if diffs:
            strike, option_type = key
            mismatches.append(
                OptionChainContractDiff(
                    strike=strike,
                    option_type=option_type,
                    field_diffs=tuple(diffs),
                )
            )

    underlying = _first_underlying(angel_quotes, nse_quotes)
    expiry = _first_expiry(angel_quotes, nse_quotes)
    return OptionChainValidationReport(
        underlying=underlying,
        expiry=expiry,
        compared_contracts=len(common_keys),
        angel_only_contracts=tuple(sorted(angel_keys - nse_keys)),
        nse_only_contracts=tuple(sorted(nse_keys - angel_keys)),
        mismatches=tuple(mismatches),
        missing_angel_iv=sum(1 for quote in angel.values() if quote.iv is None),
        missing_nse_iv=sum(1 for quote in nse.values() if quote.iv is None),
        metadata=metadata or {},
    )


def _compare_quote_fields(
    angel: OptionQuote,
    nse: OptionQuote,
    cfg: OptionChainValidationConfig,
) -> list[OptionChainFieldDiff]:
    diffs: list[OptionChainFieldDiff] = []
    field_specs = (
        ("oi", Decimal(cfg.oi_abs_tolerance), None),
        ("volume", Decimal(cfg.volume_abs_tolerance), Decimal(str(cfg.volume_pct_tolerance))),
        ("iv", Decimal(str(cfg.iv_abs_tolerance)), Decimal(str(cfg.iv_pct_tolerance))),
        ("bid", Decimal(str(cfg.price_abs_tolerance)), Decimal(str(cfg.price_pct_tolerance))),
        ("ask", Decimal(str(cfg.price_abs_tolerance)), Decimal(str(cfg.price_pct_tolerance))),
        ("ltp", Decimal(str(cfg.price_abs_tolerance)), Decimal(str(cfg.price_pct_tolerance))),
    )
    for field_name, abs_tolerance, pct_tolerance in field_specs:
        diff = _compare_field(
            field_name,
            getattr(angel, field_name),
            getattr(nse, field_name),
            abs_tolerance=abs_tolerance,
            pct_tolerance=pct_tolerance,
        )
        if diff is not None:
            diffs.append(diff)
    return diffs


def _compare_field(
    field: str,
    angel_value: Any,
    nse_value: Any,
    *,
    abs_tolerance: Decimal,
    pct_tolerance: Decimal | None,
) -> OptionChainFieldDiff | None:
    angel_decimal = _decimal(angel_value)
    nse_decimal = _decimal(nse_value)
    if angel_decimal is None or nse_decimal is None:
        if angel_decimal == nse_decimal:
            return None
        return OptionChainFieldDiff(
            field=field,
            angel_value=_stringify(angel_value),
            nse_value=_stringify(nse_value),
            tolerance=f"abs<={abs_tolerance}",
        )

    abs_diff = abs(angel_decimal - nse_decimal)
    denominator = max(abs(nse_decimal), Decimal("1"))
    pct_diff = abs_diff / denominator
    if abs_diff <= abs_tolerance:
        return None
    if pct_tolerance is not None and pct_diff <= pct_tolerance:
        return None
    tolerance = f"abs<={abs_tolerance}"
    if pct_tolerance is not None:
        tolerance = f"{tolerance} or pct<={pct_tolerance}"
    return OptionChainFieldDiff(
        field=field,
        angel_value=_stringify(angel_value),
        nse_value=_stringify(nse_value),
        abs_diff=str(abs_diff),
        pct_diff=str(pct_diff),
        tolerance=tolerance,
    )


def _key(quote: OptionQuote) -> tuple[int, str]:
    q = quote.normalized()
    return q.strike, q.option_type


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _first_underlying(*groups: Sequence[OptionQuote]) -> str:
    for group in groups:
        for quote in group:
            return quote.normalized().underlying
    return ""


def _first_expiry(*groups: Sequence[OptionQuote]) -> str:
    for group in groups:
        for quote in group:
            return quote.normalized().expiry.isoformat()
    return ""


__all__ = [
    "OptionChainContractDiff",
    "OptionChainFieldDiff",
    "OptionChainValidationConfig",
    "OptionChainValidationReport",
    "compare_angel_to_nse",
]
