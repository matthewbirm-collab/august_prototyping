import math

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from engine import odds
from engine.odds import (
    METHODS,
    InvalidOddsError,
    Method,
    american_to_decimal,
    american_to_implied_prob,
    decimal_to_american,
    devig_one_way,
    devig_two_way,
    hold,
    overround,
    power_exponent,
    shin_insider_share,
)

PROPERTY = settings(max_examples=300, deadline=None)
LOW, HIGH = 1e-4, 1 - 1e-4  # raw probabilities books actually post: odds up to about +-10,000


@st.composite
def vigged_pairs(draw: st.DrawFn) -> tuple[float, float]:
    """(over, under) raw probabilities that sum to at least 1, from no margin to absurd margins."""
    a = draw(st.floats(LOW, HIGH))
    b = draw(st.floats(max(1 - a, LOW), HIGH))
    return a, b


def approx(value: float, tolerance: float = 1e-9) -> object:
    return pytest.approx(value, abs=tolerance)


# Conversions -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("american", "decimal"),
    [
        (150, 2.5),
        (-200, 1.5),
        (100, 2.0),
        (-100, 2.0),
        (250, 3.5),
        (-110, 1 + 100 / 110),
        (-400, 1.25),
    ],
)
def test_american_to_decimal(american: int, decimal: float) -> None:
    assert american_to_decimal(american) == pytest.approx(decimal)


@pytest.mark.parametrize(
    ("decimal", "american"),
    [(2.5, 150), (1.5, -200), (2.0, 100), (3.5, 250), (1.25, -400), (1.9091, -110), (11.0, 1000)],
)
def test_decimal_to_american(decimal: float, american: int) -> None:
    assert decimal_to_american(decimal) == american


def test_every_american_price_survives_a_round_trip_through_decimal() -> None:
    prices = [*range(100, 10_001), *range(-10_000, -99)]
    for price in prices:
        expected = 100 if price == -100 else price  # even money is written +100
        assert decimal_to_american(american_to_decimal(price)) == expected, price


def test_decimal_to_american_rounds_to_the_nearest_whole_number() -> None:
    assert decimal_to_american(2.504) == 150
    assert decimal_to_american(2.506) == 151
    assert decimal_to_american(1.6667) == -150


@pytest.mark.parametrize(
    ("american", "probability"),
    [(-110, 110 / 210), (150, 0.4), (-200, 2 / 3), (100, 0.5), (-100, 0.5), (1000, 1 / 11)],
)
def test_implied_probability(american: int, probability: float) -> None:
    assert american_to_implied_prob(american) == pytest.approx(probability)


def test_implied_probability_is_the_reciprocal_of_decimal_odds() -> None:
    for price in [*range(100, 5_001, 7), *range(-5_000, -99, 7)]:
        assert american_to_implied_prob(price) == pytest.approx(1 / american_to_decimal(price))


def test_longer_prices_imply_lower_probabilities() -> None:
    ladder = [-5000, -1000, -300, -110, -100, 100, 150, 400, 2000, 10000]
    probabilities = [american_to_implied_prob(p) for p in ladder]
    assert probabilities == sorted(probabilities, reverse=True)


@pytest.mark.parametrize("bad", [0, 1, 50, -99, 99.99, -0.5, math.nan, math.inf, -math.inf])
def test_prices_between_minus_and_plus_100_are_rejected(bad: float) -> None:
    for convert in (american_to_decimal, american_to_implied_prob):
        with pytest.raises(InvalidOddsError):
            convert(bad)


@pytest.mark.parametrize("bad", [1.0, 0.5, 0.0, -2.0, math.nan, math.inf])
def test_decimal_odds_must_exceed_one(bad: float) -> None:
    with pytest.raises(InvalidOddsError):
        decimal_to_american(bad)


def test_invalid_odds_are_value_errors() -> None:
    assert issubclass(InvalidOddsError, ValueError)


@given(st.integers(100, 100_000) | st.integers(-100_000, -100))
@PROPERTY
def test_implied_probability_is_strictly_between_zero_and_one(price: int) -> None:
    assert 0 < american_to_implied_prob(price) < 1


# Margin ----------------------------------------------------------------------------------------


def test_standard_juice_has_a_4_55_percent_hold() -> None:
    a = american_to_implied_prob(-110)
    assert overround(a, a) == pytest.approx(0.047619, abs=1e-6)
    assert hold(a, a) == pytest.approx(0.045455, abs=1e-6)


def test_a_fair_pair_has_no_margin() -> None:
    assert hold(0.3, 0.7) == pytest.approx(0)
    assert overround(0.3, 0.7) == pytest.approx(0)


def test_a_pair_summing_below_one_has_negative_margin() -> None:
    assert overround(0.4, 0.5) == pytest.approx(-0.1)
    assert hold(0.4, 0.5) < 0


@given(vigged_pairs())
@PROPERTY
def test_hold_is_the_overround_as_a_share_of_the_total(pair: tuple[float, float]) -> None:
    a, b = pair
    assert hold(a, b) == approx(overround(a, b) / (1 + overround(a, b)))
    assert 0 <= hold(a, b) < 1


# De-vigging: fixed examples --------------------------------------------------------------------

# (over price, under price): fair P(over) by method. Worked by hand from the formulas in the
# module docstring: for -200/+160, a = 0.66667, b = 0.38462, S = 1.05128, so multiplicative is
# a/S, additive is a - (S-1)/2, and power solves a^k + b^k = 1 for k = 1.0828.
EXAMPLES = [
    ((-110, -110), {"multiplicative": 0.5, "additive": 0.5, "power": 0.5, "shin": 0.5}),
    (
        (-200, 160),
        {"multiplicative": 0.634146, "additive": 0.641026, "power": 0.644650, "shin": 0.641026},
    ),
    (
        (-500, 350),
        {"multiplicative": 0.789474, "additive": 0.805556, "power": 0.815013, "shin": 0.805556},
    ),
    (
        (-3000, 1200),
        {"multiplicative": 0.926366, "additive": 0.945409, "power": 0.959759, "shin": 0.945409},
    ),
    (
        (250, -350),
        {"multiplicative": 0.268657, "additive": 0.253968, "power": 0.245521, "shin": 0.253968},
    ),
]


@pytest.mark.parametrize(("prices", "expected"), EXAMPLES)
@pytest.mark.parametrize("method", METHODS)
def test_fair_probabilities_for_known_prices(
    prices: tuple[int, int], expected: dict[str, float], method: Method
) -> None:
    a, b = (american_to_implied_prob(p) for p in prices)
    over, under = devig_two_way(a, b, method)
    assert over == pytest.approx(expected[method], abs=1e-6)
    assert under == pytest.approx(1 - expected[method], abs=1e-6)


def test_the_method_decides_a_touchdown_probability_for_a_heavy_favorite() -> None:
    """Anytime TD at Yes +1200 / No -3000: the same prices, three different answers."""
    yes, no = american_to_implied_prob(1200), american_to_implied_prob(-3000)
    fair_yes = {m: devig_two_way(yes, no, m).over for m in METHODS}
    assert fair_yes["multiplicative"] == pytest.approx(0.0736, abs=5e-5)
    assert fair_yes["additive"] == pytest.approx(0.0546, abs=5e-5)
    assert fair_yes["power"] == pytest.approx(0.0402, abs=5e-5)
    assert fair_yes["shin"] == pytest.approx(fair_yes["additive"])


def test_the_default_method_is_multiplicative() -> None:
    a, b = american_to_implied_prob(-200), american_to_implied_prob(160)
    assert devig_two_way(a, b) == devig_two_way(a, b, "multiplicative")


def test_the_result_unpacks_as_over_and_under() -> None:
    over, under = devig_two_way(0.55, 0.50)
    assert (over, under) == tuple(devig_two_way(0.55, 0.50))
    assert devig_two_way(0.55, 0.50).over == over


# De-vigging: bad input -------------------------------------------------------------------------


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5, math.nan, math.inf])
def test_probabilities_outside_zero_and_one_are_rejected(method: Method, bad: float) -> None:
    with pytest.raises(InvalidOddsError):
        devig_two_way(bad, 0.6, method)
    with pytest.raises(InvalidOddsError):
        devig_two_way(0.6, bad, method)
    with pytest.raises(InvalidOddsError):
        hold(bad, 0.6)


@pytest.mark.parametrize("method", METHODS)
def test_a_pair_summing_below_one_is_rejected(method: Method) -> None:
    with pytest.raises(InvalidOddsError, match="below 1"):
        devig_two_way(0.4, 0.5, method)


@pytest.mark.parametrize("method", METHODS)
def test_float_noise_below_one_is_tolerated(method: Method) -> None:
    over, under = devig_two_way(0.5, 0.5 - 1e-12, method)
    assert over == pytest.approx(0.5, abs=1e-9)
    assert over + under == pytest.approx(1)


def test_an_unknown_method_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"unknown de-vig method 'logit'.*multiplicative"):
        devig_two_way(0.55, 0.5, "logit")  # type: ignore[arg-type]


# De-vigging: properties ------------------------------------------------------------------------


@pytest.mark.parametrize("method", METHODS)
@given(vigged_pairs())
@PROPERTY
def test_fair_probabilities_sum_to_one_and_lie_strictly_between_zero_and_one(
    method: Method, pair: tuple[float, float]
) -> None:
    over, under = devig_two_way(*pair, method)
    assert 0 < over < 1
    assert 0 < under < 1
    assert over + under == pytest.approx(1, abs=1e-12)


@pytest.mark.parametrize("method", METHODS)
@given(vigged_pairs())
@PROPERTY
def test_the_favorite_stays_the_favorite(method: Method, pair: tuple[float, float]) -> None:
    a, b = pair
    over, under = devig_two_way(a, b, method)
    if a < b - 1e-9:
        assert over < under
    elif a > b + 1e-9:
        assert over > under
    else:
        assert over == pytest.approx(under, abs=1e-6)


@pytest.mark.parametrize("method", METHODS)
@given(vigged_pairs(), st.floats(0.0, 0.5))
@PROPERTY
def test_a_higher_over_price_never_lowers_the_fair_over_probability(
    method: Method, pair: tuple[float, float], bump: float
) -> None:
    a, b = pair
    higher = min(a + bump, HIGH)
    assume(higher >= a)
    assert devig_two_way(higher, b, method).over >= devig_two_way(a, b, method).over - 1e-12


@pytest.mark.parametrize("method", METHODS)
@given(vigged_pairs())
@PROPERTY
def test_swapping_the_sides_swaps_the_answers(method: Method, pair: tuple[float, float]) -> None:
    a, b = pair
    forward = devig_two_way(a, b, method)
    backward = devig_two_way(b, a, method)
    assert backward.over == approx(forward.under, 1e-9)
    assert backward.under == approx(forward.over, 1e-9)


@pytest.mark.parametrize("method", METHODS)
@given(st.floats(LOW, HIGH))
@PROPERTY
def test_a_pair_that_is_already_fair_is_unchanged(method: Method, p: float) -> None:
    over, under = devig_two_way(p, 1 - p, method)
    assert over == approx(p, 1e-12)
    assert under == approx(1 - p, 1e-12)


@pytest.mark.parametrize("method", METHODS)
@given(vigged_pairs())
@PROPERTY
def test_de_vigging_twice_changes_nothing(method: Method, pair: tuple[float, float]) -> None:
    once = devig_two_way(*pair, method)
    twice = devig_two_way(once.over, once.under, method)
    assert twice.over == approx(once.over, 1e-12)


@given(st.floats(0.5, HIGH))
@PROPERTY
def test_methods_agree_when_the_sides_are_even(a: float) -> None:
    for method in METHODS:
        assert devig_two_way(a, a, method).over == approx(0.5, 1e-9)


# De-vigging: what defines each method ----------------------------------------------------------


@given(vigged_pairs())
@PROPERTY
def test_power_raises_both_probabilities_to_the_same_exponent_that_makes_them_sum_to_one(
    pair: tuple[float, float],
) -> None:
    a, b = pair
    k = power_exponent(a, b)
    assert k >= 1
    assert a**k + b**k == approx(1, 1e-9)
    assert devig_two_way(a, b, "power").over == approx(a**k, 1e-9)


@given(vigged_pairs())
@PROPERTY
def test_shin_probabilities_solve_shins_equation_for_the_insider_share(
    pair: tuple[float, float],
) -> None:
    a, b = pair
    z = shin_insider_share(a, b)
    assert 0 <= z < 1
    total = a + b

    def textbook(p: float) -> float:
        return (math.sqrt(z * z + 4 * (1 - z) * p * p / total) - z) / (2 * (1 - z))

    assume(z < 0.99)  # the textbook form loses precision as z nears 1; the module's does not
    assert textbook(a) + textbook(b) == approx(1, 1e-9)
    assert devig_two_way(a, b, "shin").over == approx(textbook(a), 1e-9)


@given(vigged_pairs())
@PROPERTY
def test_for_two_outcomes_shin_and_additive_are_the_same_method(pair: tuple[float, float]) -> None:
    a, b = pair
    shin = devig_two_way(a, b, "shin")
    additive = devig_two_way(a, b, "additive")
    assert shin.over == approx(additive.over, 1e-9)
    assert additive.over == approx(a - (a + b - 1) / 2, 1e-12)


@given(vigged_pairs())
@PROPERTY
def test_the_zero_margin_case_has_no_insiders_and_an_exponent_of_one(
    pair: tuple[float, float],
) -> None:
    a, _ = pair
    assert shin_insider_share(a, 1 - a) == 0
    assert power_exponent(a, 1 - a) == 1


@given(vigged_pairs())
@PROPERTY
def test_longshots_are_cut_least_by_multiplicative_and_most_by_power(
    pair: tuple[float, float],
) -> None:
    """The favorite-longshot ordering: for the favorite's fair probability,
    multiplicative <= additive (= Shin) <= power."""
    a, b = pair
    assume(abs(a - b) > 1e-6 and a + b - 1 > 1e-6)
    favorite, longshot = (a, b) if a > b else (b, a)
    fair = {m: devig_two_way(favorite, longshot, m).over for m in METHODS}
    assert fair["multiplicative"] <= fair["additive"] + 1e-12
    assert fair["additive"] <= fair["power"] + 1e-12


def test_the_solvers_cope_with_extreme_prices_and_margins() -> None:
    cases = [
        (american_to_implied_prob(-100_000), american_to_implied_prob(10_000)),
        (american_to_implied_prob(100_000), american_to_implied_prob(-100_000)),
        (american_to_implied_prob(-100), american_to_implied_prob(-100)),
        (0.999, 0.999),  # a 100% margin
        (0.9999, 0.9999),
    ]
    for a, b in cases:
        for method in METHODS:
            over, under = devig_two_way(a, b, method)
            assert 0 < over < 1 and over + under == pytest.approx(1)


def test_a_grid_of_real_price_pairs_obeys_the_rules() -> None:
    """Every pair of whole-number prices from a coarse grid, as a check beside the random ones."""
    grid = [-5000, -1500, -500, -250, -150, -110, -100, 100, 110, 150, 250, 500, 1500, 5000]
    checked = 0
    for over_price in grid:
        for under_price in grid:
            a, b = american_to_implied_prob(over_price), american_to_implied_prob(under_price)
            if a + b < 1:
                continue
            checked += 1
            fair = {m: devig_two_way(a, b, m) for m in METHODS}
            for result in fair.values():
                assert 0 < result.over < 1
                assert result.over + result.under == pytest.approx(1)
                assert (result.over <= result.under) == (a <= b) or abs(a - b) < 1e-9
            assert fair["shin"].over == pytest.approx(fair["additive"].over, abs=1e-9)
    assert checked > 60


def test_the_module_exports_what_the_spec_asks_for() -> None:
    for name in (
        "american_to_decimal",
        "decimal_to_american",
        "american_to_implied_prob",
        "devig_two_way",
        "hold",
    ):
        assert callable(getattr(odds, name))


# One-sided markets ----------------------------------------------------------------------------


def test_one_sided_multiplicative_divides_by_one_plus_the_margin() -> None:
    assert devig_one_way(0.488, 0.10) == pytest.approx(0.488 / 1.10)
    assert devig_one_way(0.488, 0.10, "additive") == pytest.approx(0.488 - 0.05)
    assert devig_one_way(0.488, 0.10, "shin") == pytest.approx(0.488 - 0.05)
    assert devig_one_way(0.488, 0.10, "power") == pytest.approx(0.43463, abs=1e-5)


@pytest.mark.parametrize("method", METHODS)
def test_no_assumed_margin_leaves_the_probability_alone(method: Method) -> None:
    assert devig_one_way(0.37, 0.0, method) == pytest.approx(0.37)


@pytest.mark.parametrize("method", METHODS)
@given(st.floats(LOW, HIGH), st.floats(0.0, 0.5))
@PROPERTY
def test_a_one_sided_fair_probability_is_below_the_raw_one_and_inside_zero_and_one(
    method: Method, p: float, margin: float
) -> None:
    fair = devig_one_way(p, margin, method)
    assert 0 < fair <= p + 1e-12


@pytest.mark.parametrize("method", METHODS)
@given(st.floats(LOW, HIGH), st.floats(0.0, 0.3), st.floats(0.0, 0.3))
@PROPERTY
def test_assuming_more_margin_never_raises_the_fair_probability(
    method: Method, p: float, low: float, extra: float
) -> None:
    assert devig_one_way(p, low + extra, method) <= devig_one_way(p, low, method) + 1e-12


@pytest.mark.parametrize("method", METHODS)
@given(st.floats(LOW, HIGH), st.floats(0.0, HIGH), st.floats(0.0, 0.3))
@PROPERTY
def test_a_shorter_price_never_lowers_the_one_sided_fair_probability(
    method: Method, p: float, bump: float, margin: float
) -> None:
    higher = min(p + bump, HIGH)
    assume(higher >= p)
    assert devig_one_way(higher, margin, method) >= devig_one_way(p, margin, method) - 1e-12


def test_a_margin_too_big_for_a_longshot_is_capped() -> None:
    """At +4900 (2%) a 10% margin would need a missing side above 100%; it is capped instead."""
    p = 0.02
    capped = devig_one_way(p, 0.10)
    assert capped == pytest.approx(p / (1 + p - 1e-4))
    assert 0.0195 < capped < p


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.2, 1.4, math.nan])
def test_one_sided_probabilities_must_be_between_zero_and_one(bad: float) -> None:
    with pytest.raises(InvalidOddsError):
        devig_one_way(bad, 0.1)


@pytest.mark.parametrize("bad", [-0.01, math.nan, math.inf])
def test_the_assumed_margin_must_be_a_finite_non_negative_number(bad: float) -> None:
    with pytest.raises(InvalidOddsError):
        devig_one_way(0.4, bad)
