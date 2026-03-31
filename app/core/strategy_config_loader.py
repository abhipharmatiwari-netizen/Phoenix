# strategy_config_loader.py
#
# Typed loaders for strategy_env.yaml and universe.yaml.
# Existing env vars are NOT overridden and nested configs stay in YAML.

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.strategies.naming import canonicalize_strategy_name
from app.strategies.validators import validate_strategy_list

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
DEFAULT_STRATEGY_ENV_PATH = CONFIG_DIR / "strategy_env.yaml"
DEFAULT_UNIVERSE_PATH = CONFIG_DIR / "universe.yaml"
AUTO_VOL_PATH = CONFIG_DIR / "auto_vol_thresholds.json"


class _StrategyInstrumentOverrideRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: str


class _StrategyConfigRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    enabled: bool
    instruments: list[str | _StrategyInstrumentOverrideRow]
    params: dict[str, Any] = Field(default_factory=dict)


class _InstrumentConfigRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool
    allowed_strategies: list[str] = Field(default_factory=list)


class _StrategySelectionConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    max_active_per_underlying: int = 1
    min_hold_seconds: float = 0.0
    mapping: dict[str, dict[str, list[str]]] = Field(default_factory=dict)


class _StrategyEnvSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    strategies: list[_StrategyConfigRow] = Field(default_factory=list)
    instruments: dict[str, _InstrumentConfigRow] = Field(default_factory=dict)
    strategy_selection: _StrategySelectionConfig | None = None


class _UniverseOptionsSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    weekly: bool | None = None
    use_atm: bool | None = None
    use_strangle_otm: bool | None = None
    otm_steps: int | list[int] | None = None
    itm_steps: int | None = None
    option_segments: list[str] = Field(default_factory=list)


class _UniverseUnderlyingSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    type: str
    segment: str
    lot_size: int
    enabled: bool
    symbol_pattern: str | None = None
    exchange: str | None = None
    index_exchange: str | None = None
    index_token: int | str | None = None
    index_symbol: str | None = None
    options: _UniverseOptionsSchema = Field(default_factory=_UniverseOptionsSchema)

    @model_validator(mode="after")
    def validate_shape(self) -> "_UniverseUnderlyingSchema":
        underlying_type = str(self.type or "").strip().upper()
        if underlying_type not in {"FUT", "INDEX", "DATA"}:
            raise ValueError("type must be FUT, INDEX, or DATA")
        self.type = underlying_type
        if self.lot_size <= 0:
            raise ValueError("lot_size must be > 0")
        if underlying_type in {"INDEX", "DATA"}:
            if not str(self.index_exchange or "").strip():
                raise ValueError("index_exchange is required for INDEX/DATA underlyings")
            if self.index_token in (None, ""):
                raise ValueError("index_token is required for INDEX/DATA underlyings")
            if not str(self.index_symbol or "").strip():
                raise ValueError("index_symbol is required for INDEX/DATA underlyings")
        if underlying_type == "FUT" and not str(self.exchange or "").strip():
            raise ValueError("exchange is required for FUT underlyings")
        return self


class _UniverseSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    underlyings: list[_UniverseUnderlyingSchema]

    @model_validator(mode="after")
    def validate_underlyings(self) -> "_UniverseSchema":
        if not self.underlyings:
            raise ValueError("underlyings must contain at least one entry")
        return self


def _set_env_if_missing(name: str, value: Any) -> None:
    """Set os.environ[name] = str(value) if not already set and value is not None."""
    if value is None:
        return
    if os.getenv(name) is not None:
        logger.debug("Env %s already set, keeping existing value", name)
        return
    os.environ[name] = str(value)
    logger.info("Env %s=%s (from YAML)", name, value)


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise

    try:
        payload = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            raise ValueError(
                f"Invalid YAML in {path} at line {mark.line + 1}, column {mark.column + 1}: {exc}"
            ) from exc
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc

    if payload and not isinstance(payload, dict):
        raise ValueError(f"YAML config {path} must be a mapping at the top level")
    return dict(payload)


def _format_validation_error(path: Path, exc: ValidationError) -> ValueError:
    details: list[str] = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err.get("loc", ())) or "<root>"
        details.append(f"{location}: {err.get('msg', 'invalid value')}")
    detail_text = "; ".join(details)
    return ValueError(f"Schema validation failed for {path}: {detail_text}")


def _validate_schema(path: Path, payload: Mapping[str, Any], schema: type[BaseModel]) -> dict[str, Any]:
    try:
        model = schema.model_validate(payload)
    except ValidationError as exc:
        raise _format_validation_error(path, exc) from exc
    return model.model_dump(exclude_none=True)


def load_universe_from_yaml(
    path: Path | str = DEFAULT_UNIVERSE_PATH,
) -> dict[str, Any]:
    resolved = Path(path)
    try:
        payload = _load_yaml_mapping(resolved)
    except FileNotFoundError:
        logger.warning("Universe config %s not found", path)
        return {}
    return _validate_schema(resolved, payload, _UniverseSchema)


def load_strategy_env_from_yaml(
    path: Path | str = DEFAULT_STRATEGY_ENV_PATH,
) -> Dict[str, Dict[str, Any]]:
    """
    Load YAML config and export each section as <SECTION>_* env vars.
    Nested 'role_labels' keys become <PREFIX>ROLE_<ROLE>_LABEL.
    """
    resolved = Path(path)
    try:
        cfg = _load_yaml_mapping(resolved)
    except FileNotFoundError:
        logger.warning("YAML config %s not found, skipping env loading", path)
        return {}

    cfg = _validate_schema(resolved, cfg, _StrategyEnvSchema)

    if (
        os.getenv("AUTO_VOL_CALIBRATION", "0").lower() in {"1", "true", "yes"}
        and AUTO_VOL_PATH.exists()
    ):
        try:
            auto_vol = json.loads(AUTO_VOL_PATH.read_text(encoding="utf-8"))
            for label, vals in auto_vol.items():
                low = vals.get("vol_low_atr_norm")
                high = vals.get("vol_high_atr_norm")
                if low is None or high is None:
                    continue
                for section_name, section in cfg.items():
                    if not isinstance(section, dict):
                        continue
                    if label.upper().startswith(section_name.upper()):
                        section["vol_low_atr_norm"] = low
                        section["vol_high_atr_norm"] = high
                        logger.info(
                            "Auto vol applied for %s: low=%.6f high=%.6f",
                            section_name,
                            low,
                            high,
                        )
        except Exception as exc:
            logger.warning("Failed to merge auto vol thresholds: %s", exc)

    strategies = cfg.get("strategies")
    if isinstance(strategies, list):
        for idx, row in enumerate(strategies):
            if not isinstance(row, dict):
                continue
            raw_name = str(row.get("name") or "").strip()
            if not raw_name:
                continue
            canonical_name = canonicalize_strategy_name(
                raw_name,
                source=f"{resolved}.strategies[{idx}].name",
                warn_alias=False,
            )
            if not canonical_name:
                raise ValueError(
                    f"Invalid strategy name in {resolved}.strategies[{idx}].name: Unknown strategy '{raw_name}'"
                )
            row["name"] = canonical_name

    instrument_cfg = cfg.get("instruments")
    if isinstance(instrument_cfg, dict):
        for underlying, row in instrument_cfg.items():
            if not isinstance(row, dict):
                continue
            allowed_raw = row.get("allowed_strategies", []) or []
            try:
                row["allowed_strategies"] = validate_strategy_list(
                    [str(x) for x in allowed_raw],
                    context=f"{resolved}.instruments.{underlying}.allowed_strategies",
                )
            except ValueError as exc:
                raise ValueError(str(exc)) from exc

    for section_name, section in cfg.items():
        if not isinstance(section, dict):
            continue
        prefix = section_name.upper() + "_"
        logger.info("Loading %s section from %s", section_name, path)

        for key, val in section.items():
            if key == "role_labels":
                role_labels = val or {}
                for role_key, role_val in role_labels.items():
                    if role_val in (None, ""):
                        continue
                    base = role_key[:-6] if str(role_key).endswith("_label") else role_key
                    role_upper = str(base).upper()
                    env_name = f"{prefix}ROLE_{role_upper}_LABEL"
                    _set_env_if_missing(env_name, role_val)
                continue
            if isinstance(val, dict):
                continue

            env_name = prefix + key.upper()
            _set_env_if_missing(env_name, val)

    return cfg


__all__ = [
    "_set_env_if_missing",
    "load_strategy_env_from_yaml",
    "load_universe_from_yaml",
]
