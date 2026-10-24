import pytest
from pydantic import ValidationError

from engine.scoring import ScoringWeights, standard_weights


def test_standard_weights_follow_plan_formula() -> None:
    ppr = standard_weights(reception=1)
    assert (ppr.pass_yds, ppr.pass_td, ppr.interception) == (0.04, 4, -2)
    assert (ppr.rush_yds, ppr.rec_yds, ppr.td, ppr.fumble_lost) == (0.1, 0.1, 6, -2)


def test_position_override_applies_only_to_that_position() -> None:
    weights = standard_weights(reception=1).model_copy(
        update={"position_overrides": {"TE": {"reception": 1.5}}}
    )
    weights = ScoringWeights.model_validate(weights.model_dump())
    assert weights.for_position("TE")["reception"] == 1.5
    assert weights.for_position("WR")["reception"] == 1


def test_override_with_unknown_stat_is_rejected() -> None:
    data = standard_weights(reception=1).model_dump()
    data["position_overrides"] = {"TE": {"receptions_typo": 1.5}}
    with pytest.raises(ValidationError, match="unknown stats"):
        ScoringWeights.model_validate(data)


def test_missing_weight_is_rejected() -> None:
    data = standard_weights(reception=1).model_dump()
    del data["pass_td"]
    with pytest.raises(ValidationError):
        ScoringWeights.model_validate(data)
