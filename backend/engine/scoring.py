"""Fantasy scoring weights.

All scoring weights come from `scoring_configs` rows validated by this model; engine code must
never hard-code them (CLAUDE.md, projection rule 3).
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

Position = Literal["QB", "RB", "WR", "TE"]


class ScoringWeights(BaseModel):
    """Points per unit of each stat, with optional per-position overrides (e.g. TE premium)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pass_yds: float
    pass_td: float
    interception: float
    rush_yds: float
    rec_yds: float
    td: float
    """Rushing or receiving touchdown."""
    reception: float
    fumble_lost: float
    two_pt: float
    position_overrides: dict[Position, dict[str, float]] = {}

    @model_validator(mode="after")
    def _overrides_name_known_stats(self) -> Self:
        stats = set(type(self).model_fields) - {"position_overrides"}
        for position, overrides in self.position_overrides.items():
            unknown = set(overrides) - stats
            if unknown:
                raise ValueError(f"unknown stats in {position} override: {sorted(unknown)}")
        return self

    def for_position(self, position: Position) -> dict[str, float]:
        """Weights for one position, with its overrides applied."""
        weights = self.model_dump(exclude={"position_overrides"})
        weights.update(self.position_overrides.get(position, {}))
        return weights


def standard_weights(reception: float) -> ScoringWeights:
    """The PLAN.md §4 formula; `reception` is 1 for PPR, 0.5 for half-PPR, 0 for standard."""
    return ScoringWeights(
        pass_yds=0.04,
        pass_td=4,
        interception=-2,
        rush_yds=0.1,
        rec_yds=0.1,
        td=6,
        reception=reception,
        fumble_lost=-2,
        two_pt=2,
    )
