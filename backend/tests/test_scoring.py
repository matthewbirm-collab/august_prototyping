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


def test_fantasy_points_score_a_box_score_line() -> None:
    from engine.scoring import fantasy_points

    line = {
        "pass_yds": 250.0,
        "pass_tds": 2.0,
        "interceptions": 1.0,
        "rush_yds": 30.0,
        "rush_tds": 1.0,
        "fumbles_lost": 1.0,
    }
    assert fantasy_points(standard_weights(1.0), "QB", line) == pytest.approx(
        10 + 8 - 2 + 3 + 6 - 2
    )
    receiver = {"receptions": 6.0, "rec_yds": 80.0, "rec_tds": 1.0, "two_pt_conversions": 1.0}
    assert fantasy_points(standard_weights(0.5), "WR", receiver) == pytest.approx(3 + 8 + 6 + 2)
    premium = standard_weights(1.0).model_copy(
        update={"position_overrides": {"TE": {"reception": 1.5}}}
    )
    assert fantasy_points(premium, "TE", receiver) - fantasy_points(premium, "WR", receiver) == (
        pytest.approx(3.0)
    )
