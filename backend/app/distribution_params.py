"""Read the fitted shapes in `config/distribution_params.json` into the engine's `StatModel`s.

The file is written by `scripts/estimate_distribution_shapes.py`.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from engine.distributions import Family, StatModel, ZeroInflation

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "distribution_params.json"
# Positions as nflverse and the books spell them, mapped to the ones the shapes are keyed by.
POSITION_ALIASES = {"HB": "RB", "FB": "RB"}


class ZeroInflationFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lines: list[float]
    shares: list[float]


class EntryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family: Family
    shape: float | None = None
    zero_inflation: ZeroInflationFile | None = None


class ParamsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    source: str
    generated: str
    note: str = ""
    stats: dict[str, dict[str, EntryFile]]


@dataclass(frozen=True)
class DistributionParams:
    """Shapes by stat and position, each stat with a `default` for positions it doesn't list."""

    models: Mapping[str, Mapping[str, StatModel]]

    def model(self, stat: str, position: str | None) -> StatModel | None:
        """The model for a stat and a player's position, or None if the stat has none."""
        by_position = self.models.get(stat)
        if by_position is None:
            return None
        key = POSITION_ALIASES.get(position or "", position or "")
        return by_position.get(key) or by_position.get("default")


def load_distribution_params(path: Path = DEFAULT_PATH) -> DistributionParams:
    """Load and check the file. Raises ValueError with the file name if it is malformed."""
    try:
        parsed = ParamsFile.model_validate(json.loads(path.read_text()))
        models = {
            stat: {position: _model(entry) for position, entry in by_position.items()}
            for stat, by_position in parsed.stats.items()
        }
    except (ValidationError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{path.name} is not valid: {exc}") from exc
    return DistributionParams(models)


def _model(entry: EntryFile) -> StatModel:
    zero = (
        ZeroInflation(tuple(entry.zero_inflation.lines), tuple(entry.zero_inflation.shares))
        if entry.zero_inflation
        else None
    )
    return StatModel(entry.family, entry.shape, zero)
