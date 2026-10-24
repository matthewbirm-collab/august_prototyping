import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine.distributions import StatModel, ZeroInflation, fit_anytime_td, fit_line
from engine.projection import (
    POSITION_STATS,
    StatInput,
    project,
)
from engine.scoring import Position, ScoringWeights, standard_weights

GAMMA = StatModel("gamma", 3.0, ZeroInflation((10.0, 40.0), (0.15, 0.02)))
QB_YARDS = StatModel("gamma", 10.0)
POISSON = StatModel("poisson")
PPR, HALF, STANDARD = (standard_weights(r) for r in (1.0, 0.5, 0.0))


def stat(
    model: StatModel,
    line: float,
    p: float = 0.5,
    books: int = 4,
    dispersion: float | None = 0.0,
    age: float = 0.0,
) -> StatInput:
    return StatInput(fit_line(model, line, p), books, dispersion, line, age)


def anytime(
    p: float, books: int = 4, dispersion: float | None = 0.0, age: float = 0.0
) -> StatInput:
    return StatInput(fit_anytime_td(p), books, dispersion, None, age)


def receiver(**changes: StatInput) -> dict[str, StatInput]:
    base = {
        "rec_yds": stat(GAMMA, 52.5),
        "receptions": stat(POISSON, 4.5, 0.55),
        "anytime_td": anytime(0.45),
    }
    return {**base, **changes}


def quarterback() -> dict[str, StatInput]:
    return {
        "pass_yds": stat(QB_YARDS, 248.5),
        "pass_tds": stat(POISSON, 1.5, 0.5),
        "interceptions": stat(POISSON, 0.5, 0.45),
        "rush_yds": stat(GAMMA, 18.5),
        "anytime_td": anytime(0.12),
    }


# The formula ------------------------------------------------------------------------------------


def test_a_receiver_is_scored_on_stat_means_with_the_configured_weights() -> None:
    inputs = receiver()
    result = project("WR", PPR, inputs, 0.02)

    expected = (
        0.1 * inputs["rec_yds"].distribution.mean
        + 1.0 * inputs["receptions"].distribution.mean
        + 6.0 * inputs["anytime_td"].distribution.mean
        - 2.0 * 0.02
    )
    assert result.mean == pytest.approx(expected)
    assert result.breakdown["rec_yds"].points == pytest.approx(
        0.1 * inputs["rec_yds"].distribution.mean
    )
    assert sum(b.points for b in result.breakdown.values()) == pytest.approx(result.mean)


def test_the_mean_is_above_the_line_for_skewed_yardage() -> None:
    result = project("WR", PPR, receiver(), 0.0)
    assert result.breakdown["rec_yds"].mean > 52.5


def test_the_three_standard_formats_differ_by_the_reception_weight_only() -> None:
    inputs = receiver()
    receptions = inputs["receptions"].distribution.mean
    ppr, half, std = (project("WR", w, inputs, 0.0).mean for w in (PPR, HALF, STANDARD))
    assert ppr - half == pytest.approx(0.5 * receptions)
    assert half - std == pytest.approx(0.5 * receptions)


def test_a_quarterbacks_touchdowns_split_between_passing_and_rushing_props() -> None:
    inputs = quarterback()
    result = project("QB", PPR, inputs, 0.0)
    assert result.breakdown["pass_tds"].weight == 4.0
    assert result.breakdown["anytime_td"].weight == 6.0
    assert result.breakdown["pass_tds"].points == pytest.approx(
        4.0 * inputs["pass_tds"].distribution.mean
    )
    assert result.breakdown["interceptions"].points < 0


def test_the_anytime_td_mean_is_the_poisson_rate() -> None:
    result = project("RB", PPR, {"anytime_td": anytime(0.4)}, 0.0)
    # lambda = -ln(0.6): the 6 points are paid on expected touchdowns, not on the probability.
    assert result.breakdown["anytime_td"].mean == pytest.approx(0.5108256)
    assert result.mean == pytest.approx(6 * 0.5108256)


def test_markets_outside_the_positions_stats_are_not_added() -> None:
    """A receiver's rushing prop is ignored, and nothing is paid twice for touchdowns."""
    with_rush = receiver(rush_yds=stat(GAMMA, 20.5))
    assert project("WR", PPR, with_rush, 0.0).mean == project("WR", PPR, receiver(), 0.0).mean
    assert "rush_yds" not in project("WR", PPR, with_rush, 0.0).breakdown
    assert set(POSITION_STATS["RB"]) == {"rush_yds", "rec_yds", "receptions", "anytime_td"}


def test_fumbles_lost_come_from_the_rate_and_the_weight() -> None:
    clean = project("RB", PPR, {"anytime_td": anytime(0.4)}, 0.0)
    fumbly = project("RB", PPR, {"anytime_td": anytime(0.4)}, 0.1)
    assert clean.mean - fumbly.mean == pytest.approx(0.2)
    assert fumbly.breakdown["fumbles_lost"].source == "history"
    assert fumbly.breakdown["fumbles_lost"].points == pytest.approx(-0.2)


# Custom scoring configs ---------------------------------------------------------------------------


def test_a_six_point_passing_td_changes_only_the_passing_td_points() -> None:
    inputs = quarterback()
    six = standard_weights(1.0).model_copy(update={"pass_td": 6.0})
    base = project("QB", PPR, inputs, 0.1)
    custom = project("QB", six, inputs, 0.1)
    assert custom.mean - base.mean == pytest.approx(2.0 * inputs["pass_tds"].distribution.mean)
    for name in ("pass_yds", "interceptions", "rush_yds", "anytime_td", "fumbles_lost"):
        assert custom.breakdown[name].points == pytest.approx(base.breakdown[name].points)


def test_a_te_premium_applies_to_tight_ends_only() -> None:
    premium = standard_weights(1.0).model_copy(
        update={"position_overrides": {"TE": {"reception": 1.5}}}
    )
    inputs = receiver()
    receptions = inputs["receptions"].distribution.mean
    assert project("TE", premium, inputs, 0.0).mean - project("TE", PPR, inputs, 0.0).mean == (
        pytest.approx(0.5 * receptions)
    )
    assert project("WR", premium, inputs, 0.0).mean == pytest.approx(
        project("WR", PPR, inputs, 0.0).mean
    )


def test_engine_has_no_hard_coded_weights() -> None:
    """Zero every weight and nothing is left: the weights are the only source of points."""
    zero = ScoringWeights(
        pass_yds=0,
        pass_td=0,
        interception=0,
        rush_yds=0,
        rec_yds=0,
        td=0,
        reception=0,
        fumble_lost=0,
        two_pt=0,
    )
    result = project("WR", zero, receiver(), 0.3)
    assert result.mean == 0
    assert (result.p20, result.p80) == (0, 0)


# The range -------------------------------------------------------------------------------------


def test_the_range_surrounds_the_mean_and_is_repeatable() -> None:
    a = project("WR", PPR, receiver(), 0.02)
    b = project("WR", PPR, receiver(), 0.02)
    assert a == b
    assert 0 <= a.p20 < a.mean < a.p80
    assert a.p80 - a.p20 > 5


def test_a_wider_scoring_spread_for_a_bigger_stat_widens_the_range() -> None:
    narrow = project("WR", HALF, {"receptions": stat(POISSON, 4.5)}, 0.0)
    wide = project("WR", PPR, {"receptions": stat(POISSON, 4.5)}, 0.0)
    assert wide.p80 - wide.p20 >= narrow.p80 - narrow.p20


def test_a_negative_weight_does_not_flip_the_range() -> None:
    result = project("QB", PPR, {"interceptions": stat(POISSON, 0.5, 0.45)}, 0.0)
    assert result.p20 <= result.p80
    assert result.p80 <= 0


def test_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="fumble"):
        project("WR", PPR, receiver(), -0.1)
    with pytest.raises(ValueError, match="stale_minutes"):
        project("WR", PPR, receiver(), 0.0, stale_minutes=0)


# Confidence ------------------------------------------------------------------------------------


def score(**changes: StatInput) -> float:
    return project("WR", PPR, receiver(**changes), 0.0).confidence.score


def test_full_markets_fresh_agreeing_quotes_are_fully_confident() -> None:
    confidence = project("WR", PPR, receiver(), 0.0).confidence
    assert confidence.score == pytest.approx(1.0)
    assert confidence.missing == ()


def test_a_missing_market_lowers_confidence_by_the_points_it_carries() -> None:
    inputs = receiver()
    del inputs["rec_yds"]
    confidence = project("WR", PPR, inputs, 0.0).confidence
    assert confidence.missing == ("rec_yds",)
    assert "rec_yds" not in project("WR", PPR, inputs, 0.0).breakdown
    assert 0.3 < confidence.missing_markets < 0.9
    # Yardage carries more of a receiver's score than the touchdown market.
    no_td = receiver()
    del no_td["anytime_td"]
    assert (
        project("WR", PPR, inputs, 0.0).confidence.score
        != project("WR", PPR, no_td, 0.0).confidence.score
    )


def test_a_stat_the_scoring_config_does_not_pay_costs_nothing_when_missing() -> None:
    no_ppr = receiver()
    del no_ppr["receptions"]
    assert project("WR", STANDARD, no_ppr, 0.0).confidence.missing_markets == pytest.approx(1.0)
    assert project("WR", PPR, no_ppr, 0.0).confidence.missing_markets < 1.0


def test_no_markets_at_all_is_zero_confidence() -> None:
    result = project("WR", PPR, {}, 0.05)
    assert result.confidence.score == 0
    assert result.mean == pytest.approx(-0.1)


def test_disagreeing_books_lower_confidence() -> None:
    agree = score(rec_yds=stat(GAMMA, 52.5, dispersion=0.5))
    split = score(rec_yds=stat(GAMMA, 52.5, dispersion=8.0))
    assert agree > split
    assert score(rec_yds=stat(GAMMA, 52.5, dispersion=5.25)) < agree  # a tenth of the line


def test_few_books_lower_confidence() -> None:
    assert score(rec_yds=stat(GAMMA, 52.5, books=1)) < score(rec_yds=stat(GAMMA, 52.5, books=2))
    assert score(rec_yds=stat(GAMMA, 52.5, books=2)) < score(rec_yds=stat(GAMMA, 52.5, books=3))
    assert score(rec_yds=stat(GAMMA, 52.5, books=3)) == score(rec_yds=stat(GAMMA, 52.5, books=9))


def test_a_single_book_has_no_dispersion_penalty_of_its_own() -> None:
    assert score(rec_yds=stat(GAMMA, 52.5, books=1, dispersion=None)) == score(
        rec_yds=stat(GAMMA, 52.5, books=1, dispersion=0.0)
    )


def test_old_quotes_lower_confidence_down_to_a_floor() -> None:
    fresh = score(rec_yds=stat(GAMMA, 52.5, age=0))
    old = score(rec_yds=stat(GAMMA, 52.5, age=45))
    stale = score(rec_yds=stat(GAMMA, 52.5, age=90))
    ancient = score(rec_yds=stat(GAMMA, 52.5, age=900))
    assert fresh > old > stale
    assert ancient == stale


def test_probability_dispersion_is_scaled_for_lineless_markets() -> None:
    steady = score(anytime_td=anytime(0.45, dispersion=0.0))
    shaky = score(anytime_td=anytime(0.45, dispersion=0.05))
    assert shaky < steady


# Properties -------------------------------------------------------------------------------------

positive = st.floats(min_value=0.0, max_value=10.0)


@settings(max_examples=40, deadline=None)
@given(
    scale=st.floats(min_value=0.1, max_value=5.0),
    line=st.floats(min_value=20.0, max_value=120.0).map(lambda x: int(x) + 0.5),
    p=st.floats(min_value=0.3, max_value=0.7),
    fumbles=st.floats(min_value=0.0, max_value=0.5),
)
def test_scaling_every_weight_scales_the_projection(
    scale: float, line: float, p: float, fumbles: float
) -> None:
    inputs = receiver(rec_yds=stat(GAMMA, line, p))
    scaled = ScoringWeights.model_validate(
        {k: v * scale for k, v in PPR.model_dump(exclude={"position_overrides"}).items()}
    )
    base = project("WR", PPR, inputs, fumbles)
    other = project("WR", scaled, inputs, fumbles)
    assert other.mean == pytest.approx(scale * base.mean)
    assert other.p20 == pytest.approx(scale * base.p20, rel=1e-6, abs=1e-9)
    assert other.p80 == pytest.approx(scale * base.p80, rel=1e-6, abs=1e-9)
    assert base.p20 <= base.p80
    assert 0 <= base.confidence.score <= 1


# Correlated ranges --------------------------------------------------------------------------------

LINKED: dict[Position, dict[tuple[str, str], float]] = {
    "WR": {("rec_yds", "receptions"): 0.8, ("rec_yds", "anytime_td"): 0.4}
}


def test_correlated_draws_keep_the_mean_and_widen_the_range() -> None:
    alone = project("WR", PPR, receiver(), 0.0, correlations={"WR": {}})
    linked = project("WR", PPR, receiver(), 0.0, correlations=LINKED)
    assert linked.mean == alone.mean
    assert linked.p80 - linked.p20 > (alone.p80 - alone.p20) * 1.1


def test_the_volume_draw_widens_only_modelled_stats() -> None:
    from engine.gapfill import distribution

    modelled = {
        "rec_yds": StatInput(distribution(GAMMA, 40.0), 0, source="model"),
        "receptions": StatInput(distribution(POISSON, 3.5), 0, source="model"),
    }
    still = project("WR", PPR, modelled, 0.0, volume_cv=0.0)
    shaky = project("WR", PPR, modelled, 0.0, volume_cv=0.4)
    assert shaky.mean == still.mean
    assert shaky.p80 - shaky.p20 > still.p80 - still.p20
    markets = project("WR", PPR, receiver(), 0.0, volume_cv=0.4)
    assert (markets.p20, markets.p80) == (
        project("WR", PPR, receiver(), 0.0, volume_cv=0.0).p20,
        project("WR", PPR, receiver(), 0.0, volume_cv=0.0).p80,
    )
    with pytest.raises(ValueError, match="volume_cv"):
        project("WR", PPR, modelled, 0.0, volume_cv=-0.1)


def test_inconsistent_correlations_are_made_valid() -> None:
    bad: dict[Position, dict[tuple[str, str], float]] = {
        "WR": {
            ("rec_yds", "receptions"): 0.95,
            ("rec_yds", "anytime_td"): 0.95,
            ("receptions", "anytime_td"): -0.95,
        }
    }
    result = project("WR", PPR, receiver(), 0.0, correlations=bad)
    assert result.p20 <= result.mean <= result.p80
