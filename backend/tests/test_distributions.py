import math

import numpy as np
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from scipy import stats

from engine.distributions import (
    FittedLineModel,
    LinePoint,
    StatModel,
    ZeroInflation,
    fit,
    fit_anytime_td,
    fit_line,
    line_slope,
    shift_probability,
)

PROPERTY = settings(max_examples=150, deadline=None)
ZI = ZeroInflation((10.0, 20.0, 40.0, 60.0), (0.20, 0.15, 0.08, 0.01))

GAMMA = StatModel("gamma", 3.0)
GAMMA_ZI = StatModel("gamma", 3.0, ZI)
LOGNORMAL = StatModel("lognormal", 0.7)
LOGNORMAL_ZI = StatModel("lognormal", 0.7, ZI)
POISSON = StatModel("poisson")
NEGBIN = StatModel("negbin", 16.0)
YARDAGE = [GAMMA, GAMMA_ZI, LOGNORMAL, LOGNORMAL_ZI]
COUNTS = [POISSON, NEGBIN]
EVERY_MODEL = [*YARDAGE, *COUNTS]
MODEL_IDS = ["gamma", "gamma+zero", "lognormal", "lognormal+zero", "poisson", "negbin"]

probabilities = st.floats(0.02, 0.98)
yard_lines = st.floats(5, 300)
half_lines = st.integers(0, 40).map(lambda k: k + 0.5)
whole_lines = st.integers(1, 40).map(float)


# Models ----------------------------------------------------------------------------------------


def test_zero_inflation_interpolates_in_the_log_of_the_line() -> None:
    assert ZI.at(10) == 0.20
    assert ZI.at(60) == 0.01
    assert ZI.at(1) == 0.20 and ZI.at(500) == 0.01  # the end values hold
    midpoint = ZI.at(math.sqrt(20 * 40))  # halfway between 20 and 40 in ln(line)
    assert midpoint == pytest.approx((0.15 + 0.08) / 2)


@pytest.mark.parametrize(
    "args",
    [
        ((), ()),
        ((10.0, 20.0), (0.1,)),
        ((20.0, 10.0), (0.1, 0.2)),
        ((10.0, 10.0), (0.1, 0.2)),
        ((0.0, 10.0), (0.1, 0.2)),
        ((10.0,), (1.0,)),
        ((10.0,), (-0.1,)),
    ],
)
def test_bad_zero_inflation_is_rejected(args: tuple[tuple[float, ...], tuple[float, ...]]) -> None:
    with pytest.raises(ValueError):
        ZeroInflation(*args)


@pytest.mark.parametrize(
    "build",
    [
        lambda: StatModel("gamma"),
        lambda: StatModel("gamma", 0),
        lambda: StatModel("lognormal", -1),
        lambda: StatModel("negbin"),
        lambda: StatModel("poisson", 2.0),
        lambda: StatModel("bernoulli", 1.0),
        lambda: StatModel("poisson", None, ZI),
    ],
)
def test_bad_models_are_rejected(build: object) -> None:
    with pytest.raises(ValueError):
        build()  # type: ignore[operator]


# Exact by construction -------------------------------------------------------------------------


@pytest.mark.parametrize("model", YARDAGE, ids=MODEL_IDS[:4])
@given(yard_lines, probabilities)
@PROPERTY
def test_a_yardage_fit_reproduces_the_probability_at_its_line(
    model: StatModel, line: float, p: float
) -> None:
    fitted = fit_line(model, line, p)
    assert fitted.p_over(line) == pytest.approx(p, abs=1e-9)
    assert fitted.mean > 0
    assert fitted.quantile(0.2) <= fitted.median <= fitted.quantile(0.8)


@pytest.mark.parametrize("model", COUNTS, ids=MODEL_IDS[4:])
@given(half_lines, probabilities)
@PROPERTY
def test_a_count_fit_reproduces_the_probability_at_a_half_point_line(
    model: StatModel, line: float, p: float
) -> None:
    assert fit_line(model, line, p).p_over(line) == pytest.approx(p, abs=1e-9)


@pytest.mark.parametrize("model", COUNTS, ids=MODEL_IDS[4:])
@given(whole_lines, probabilities)
@PROPERTY
def test_a_count_fit_reproduces_the_probability_at_a_whole_number_line(
    model: StatModel, line: float, p: float
) -> None:
    assert fit_line(model, line, p).p_over(line) == pytest.approx(p, abs=1e-7)


@pytest.mark.parametrize("model", EVERY_MODEL, ids=MODEL_IDS)
@given(st.floats(8, 120), st.floats(0.05, 0.6), st.floats(0.05, 0.4))
@PROPERTY
def test_a_higher_probability_means_a_higher_mean(
    model: StatModel, line: float, low: float, extra: float
) -> None:
    line = math.floor(line) + 0.5 if not model.continuous else line
    low_fit, high_fit = fit_line(model, line, low), fit_line(model, line, low + extra)
    assert high_fit.mean >= low_fit.mean - 1e-12


# Half-point lines against independent calculations ---------------------------------------------


def test_poisson_over_a_half_point_means_that_many_plus_one_or_more() -> None:
    fitted = fit_line(POISSON, 4.5, 0.55)
    lam = fitted.location
    below = sum(math.exp(-lam) * lam**k / math.factorial(k) for k in range(5))  # 0 to 4
    assert 1 - below == pytest.approx(0.55, abs=1e-12)
    assert fitted.sf(4.5) == pytest.approx(0.55, abs=1e-12)
    assert fitted.sf(4) == fitted.sf(4.5)  # over 4 and over 4.5 are the same event for a count


def test_negative_binomial_matches_the_probability_mass_formula() -> None:
    r = NEGBIN.shape
    assert r is not None
    fitted = fit_line(NEGBIN, 22.5, 0.5)
    mean = fitted.location
    p_success = r / (r + mean)

    def pmf(k: int) -> float:
        log_pmf = (
            math.lgamma(k + r)
            - math.lgamma(r)
            - math.lgamma(k + 1)
            + r * math.log(p_success)
            + k * math.log(1 - p_success)
        )
        return math.exp(log_pmf)

    assert 1 - sum(pmf(k) for k in range(23)) == pytest.approx(0.5, abs=1e-10)


def test_a_whole_number_line_is_conditional_on_no_push() -> None:
    fitted = fit_line(POISSON, 5.0, 0.5)
    lam = fitted.location
    over, push = stats.poisson.sf(5, lam), stats.poisson.pmf(5, lam)
    assert over / (1 - push) == pytest.approx(0.5, abs=1e-10)
    assert fitted.sf(5.0) == pytest.approx(over)  # the plain tail is not conditional


def test_a_count_line_below_one_is_fine() -> None:
    assert fit_line(POISSON, 0.5, 0.45).p_over(0.5) == pytest.approx(0.45)
    with pytest.raises(ValueError, match="zero"):
        fit_line(POISSON, 0.0, 0.45)


# Anytime TD ------------------------------------------------------------------------------------


@given(st.floats(0.001, 0.999))
@PROPERTY
def test_anytime_td_rate_is_minus_the_log_of_the_complement(p: float) -> None:
    fitted = fit_anytime_td(p)
    assert fitted.location == pytest.approx(-math.log(1 - p), rel=1e-9)
    assert fitted.mean == pytest.approx(-math.log(1 - p), rel=1e-9)
    assert 1 - stats.poisson.pmf(0, fitted.location) == pytest.approx(
        p, abs=1e-9
    )  # P(at least one)


def test_the_bernoulli_family_is_the_anytime_td_model() -> None:
    model = StatModel("bernoulli")
    assert fit_line(model, 0.5, 0.4).location == pytest.approx(-math.log(0.6))
    assert fit(
        model, [LinePoint(0.5, 0.4, 1.0), LinePoint(0.5, 0.5, 3.0)]
    ).location == pytest.approx(-math.log(1 - 0.475))


# Zero-inflation --------------------------------------------------------------------------------


def test_zero_inflated_yardage_puts_that_share_of_games_at_zero() -> None:
    fitted = fit_line(GAMMA_ZI, 24.5, 0.5)
    assert fitted.zero_mass == pytest.approx(ZI.at(24.5))
    assert fitted.cdf(0) == pytest.approx(fitted.zero_mass)
    assert fitted.p_over(24.5) == pytest.approx(0.5)
    assert fitted.quantile(fitted.zero_mass * 0.9) == 0.0
    assert fitted.quantile(fitted.zero_mass + 0.01) > 0


def test_zeros_make_the_same_line_probability_mean_a_bigger_player() -> None:
    """A 50% shot at 24.5 with 14% zero games needs a better player than one with no zeros."""
    plain, inflated = fit_line(GAMMA, 24.5, 0.5), fit_line(GAMMA_ZI, 24.5, 0.5)
    assert inflated.location > plain.location


def test_the_zero_share_is_cut_back_when_the_probability_would_be_unreachable() -> None:
    fitted = fit_line(GAMMA_ZI, 12.5, 0.97)  # 17% zeros could never leave 97% above the line
    assert fitted.zero_mass < 0.03
    assert fitted.p_over(12.5) == pytest.approx(0.97, abs=1e-9)


# Moments and quantiles against simulation ------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "line", "p"),
    [
        (GAMMA, 64.5, 0.52),
        (GAMMA_ZI, 24.5, 0.5),
        (LOGNORMAL, 250.5, 0.55),
        (LOGNORMAL_ZI, 18.5, 0.45),
        (POISSON, 4.5, 0.55),
        (NEGBIN, 22.5, 0.5),
    ],
    ids=["gamma", "gamma+zero", "lognormal", "lognormal+zero", "poisson", "negbin"],
)
def test_mean_variance_and_quantiles_agree_with_simulation(
    model: StatModel, line: float, p: float
) -> None:
    fitted = fit_line(model, line, p)
    draws = fitted.sample(2_000_000, np.random.default_rng(20261024))
    scale = float(draws.std())

    assert draws.mean() == pytest.approx(fitted.mean, abs=0.01 * scale)
    assert draws.var() == pytest.approx(fitted.variance, rel=0.02)
    assert (draws > line).mean() == pytest.approx(fitted.sf(line), abs=0.002)
    if model.continuous:
        assert (draws == 0).mean() == pytest.approx(fitted.zero_mass, abs=0.002)
    for q in (0.2, 0.5, 0.8):
        x = fitted.quantile(q)
        if model.continuous:
            assert x == pytest.approx(float(np.quantile(draws, q)), abs=0.03 * scale)
        else:  # the smallest count whose cumulative share reaches q
            assert (draws <= x).mean() >= q - 0.003
            assert (draws <= x - 1).mean() <= q + 0.003
    summary = fitted.summary()
    assert (summary.mean, summary.median) == (fitted.mean, fitted.quantile(0.5))
    assert summary.p20 <= summary.median <= summary.p80


@pytest.mark.parametrize("model", EVERY_MODEL, ids=MODEL_IDS)
def test_the_quantile_is_the_inverse_of_the_cdf(model: StatModel) -> None:
    line = 24.5
    fitted = fit_line(model, line, 0.5)
    previous = -1.0
    for q in (0.05, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95):
        x = fitted.quantile(q)
        assert x >= previous
        previous = x
        assert fitted.cdf(x) >= q - 1e-9  # at or above q at the quantile...
        if model.continuous and x > 0:
            assert fitted.cdf(x) == pytest.approx(q, abs=1e-7)
        elif not model.continuous:
            assert fitted.cdf(x - 1) < q  # ...and below q one step before


@pytest.mark.parametrize("model", EVERY_MODEL, ids=MODEL_IDS)
def test_outcomes_from_the_distribution_are_uniform_after_the_probability_transform(
    model: StatModel,
) -> None:
    fitted = fit_line(model, 24.5, 0.5)
    draws = fitted.sample(100_000, np.random.default_rng(7))
    pits = np.array([fitted.pit(float(x)) for x in draws[:20_000]])
    assert pits.mean() == pytest.approx(0.5, abs=0.01)
    assert pits.std() == pytest.approx(math.sqrt(1 / 12), abs=0.01)
    assert (pits < 0.2).mean() == pytest.approx(0.2, abs=0.015)


def test_sampling_is_reproducible_from_the_generator() -> None:
    fitted = fit_line(GAMMA_ZI, 24.5, 0.5)
    a = fitted.sample(50, np.random.default_rng(1))
    b = fitted.sample(50, np.random.default_rng(1))
    assert (a == b).all()


def test_bad_quantiles_and_lines_are_rejected() -> None:
    fitted = fit_line(GAMMA, 64.5, 0.5)
    for q in (0.0, 1.0, -0.1):
        with pytest.raises(ValueError):
            fitted.quantile(q)
    with pytest.raises(ValueError, match="positive"):
        fit_line(GAMMA, 0.0, 0.5)
    with pytest.raises(ValueError, match="negative"):
        fit_line(POISSON, -0.5, 0.5)


# Several lines at once -------------------------------------------------------------------------


def alternate_points(
    truth: StatModel, location: float, lines: list[float], weights: list[float] | None = None
) -> list[LinePoint]:
    from engine.distributions import FittedDistribution

    distribution = FittedDistribution(truth, location)
    weights = weights or [1.0] * len(lines)
    return [
        LinePoint(line, distribution.p_over(line), w)
        for line, w in zip(lines, weights, strict=True)
    ]


def test_a_lognormal_fit_recovers_location_and_shape_from_exact_points() -> None:
    truth = StatModel("lognormal", 0.55)
    points = alternate_points(truth, math.log(70), [30.5, 50.5, 70.5, 100.5, 130.5])
    fitted = fit(StatModel("lognormal", 0.7), points, prior_strength=0.0)

    assert fitted.model.shape == pytest.approx(0.55, rel=1e-6)
    assert fitted.location == pytest.approx(math.log(70), abs=1e-6)


def test_the_prior_pulls_the_shape_toward_its_configured_value() -> None:
    truth = StatModel("lognormal", 0.55)
    points = alternate_points(truth, math.log(70), [30.5, 50.5, 70.5, 100.5, 130.5])
    shape = {
        tau: fit(StatModel("lognormal", 0.7), points, tau).model.shape for tau in (0.0, 0.25, 4.0)
    }
    assert shape[0.0] is not None and shape[0.25] is not None and shape[4.0] is not None
    assert shape[0.0] == pytest.approx(0.55)
    assert 0.55 < shape[0.25] < shape[4.0] < 0.7


def test_the_fitted_shape_stays_within_a_factor_of_two_of_the_configured_one() -> None:
    # Points far flatter than any realistic spread would give a huge sigma without the bound.
    points = [LinePoint(40.5, 0.52), LinePoint(80.5, 0.50), LinePoint(120.5, 0.48)]
    fitted = fit(StatModel("lognormal", 0.7), points, prior_strength=0.0)
    assert fitted.model.shape == pytest.approx(1.4)
    # And points that rise with the line are nonsense: the configured shape is kept.
    rising = fit(StatModel("lognormal", 0.7), [LinePoint(40.5, 0.3), LinePoint(80.5, 0.6)], 0.0)
    assert rising.model.shape == pytest.approx(0.7)


@pytest.mark.parametrize(
    ("family", "shape", "location", "lines"),
    [
        ("gamma", 3.0, 25.0, [30.5, 50.5, 75.5, 100.5, 140.5]),
        ("negbin", 16.0, 13.0, [8.5, 10.5, 12.5, 14.5, 17.5]),
    ],
)
def test_gamma_and_negative_binomial_fits_recover_the_truth_from_exact_points(
    family: str, shape: float, location: float, lines: list[float]
) -> None:
    truth = StatModel(family, shape)  # type: ignore[arg-type]
    points = alternate_points(truth, location, lines)
    fitted = fit(StatModel(family, shape * 1.4), points, prior_strength=0.0)  # type: ignore[arg-type]

    assert fitted.model.shape == pytest.approx(shape, rel=1e-3)
    assert fitted.location == pytest.approx(location, rel=1e-3)
    for point in points:
        assert fitted.p_over(point.line) == pytest.approx(point.p_over, abs=1e-4)


def test_a_poisson_fit_over_several_lines_is_weighted_toward_the_main_line() -> None:
    main, tail = LinePoint(4.5, 0.60, 5.0), LinePoint(6.5, 0.20, 1.0)
    fitted = fit(POISSON, [main, tail])
    single = fit_line(POISSON, 4.5, 0.60)
    assert fitted.location == pytest.approx(single.location, rel=0.06)
    assert abs(fitted.p_over(4.5) - 0.60) < abs(fitted.p_over(6.5) - 0.20) + 0.05


def test_points_on_the_same_line_are_averaged_and_zero_weights_ignored() -> None:
    a = fit(
        GAMMA, [LinePoint(64.5, 0.4, 1.0), LinePoint(64.5, 0.6, 1.0), LinePoint(90.5, 0.1, 0.0)]
    )
    assert a.location == pytest.approx(fit_line(GAMMA, 64.5, 0.5).location)
    with pytest.raises(ValueError, match="at least one"):
        fit(GAMMA, [])
    with pytest.raises(ValueError, match="at least one"):
        fit(GAMMA, [LinePoint(64.5, 0.5, 0.0)])


def test_the_zero_share_for_a_joint_fit_is_read_at_the_main_line() -> None:
    points = [LinePoint(24.5, 0.5, 3.0), LinePoint(49.5, 0.2, 1.0)]
    fitted = fit(GAMMA_ZI, points)
    assert fitted.zero_mass == pytest.approx(ZI.at(24.5), abs=1e-9)


# Moving between lines --------------------------------------------------------------------------


@pytest.mark.parametrize("model", YARDAGE, ids=MODEL_IDS[:4])
@given(st.floats(20, 200), st.floats(-10, 10), st.floats(0.02, 0.75))
@PROPERTY
def test_shifting_a_yardage_probability_and_back_returns_the_original(
    model: StatModel, line: float, delta: float, p: float
) -> None:
    target = line + delta
    assume(target > 5)
    there = shift_probability(model, p, line, target)
    # The zero share is the same both ways, so the round trip is exact.
    assert shift_probability(model, there, target, line) == pytest.approx(p, abs=1e-6)


def test_the_zero_share_for_a_shift_is_shared_not_read_at_each_books_own_line() -> None:
    reference = shift_probability(GAMMA_ZI, 0.5, 20.0, 24.5, zero_at=24.5)
    default = shift_probability(GAMMA_ZI, 0.5, 20.0, 24.5)
    elsewhere = shift_probability(GAMMA_ZI, 0.5, 20.0, 24.5, zero_at=10.0)
    assert len({round(reference, 9), round(default, 9), round(elsewhere, 9)}) == 3
    # The consensus view shifts every book to the reference line with the share read there.
    assert FittedLineModel(GAMMA_ZI).shift(0.5, 20.0, 24.5) == pytest.approx(reference)


@pytest.mark.parametrize(
    "model", [*COUNTS, StatModel("gamma", 3.0)], ids=["poisson", "negbin", "gamma"]
)
@given(half_lines.filter(lambda x: 3.5 <= x <= 30), st.integers(-2, 2), probabilities)
@PROPERTY
def test_shifting_a_count_probability_and_back_is_exact(
    model: StatModel, line: float, steps: int, p: float
) -> None:
    target = line + steps
    assume(target > 0)
    back = shift_probability(model, shift_probability(model, p, line, target), target, line)
    assert back == pytest.approx(p, abs=1e-6)


@pytest.mark.parametrize("model", EVERY_MODEL, ids=MODEL_IDS)
def test_moving_the_line_up_lowers_the_probability_and_down_raises_it(model: StatModel) -> None:
    line = 24.5
    assert shift_probability(model, 0.5, line, line) == 0.5
    assert shift_probability(model, 0.5, line, line + 3) < 0.5
    assert shift_probability(model, 0.5, line, line - 3) > 0.5


@pytest.mark.parametrize("model", EVERY_MODEL, ids=MODEL_IDS)
def test_an_absurd_shift_cannot_make_a_certainty(model: StatModel) -> None:
    assert 0 < shift_probability(model, 0.5, 24.5, 900.5) < 1
    assert 0 < shift_probability(model, 0.5, 24.5, 0.5) < 1


def test_a_count_moves_by_the_probability_of_the_value_between_the_lines() -> None:
    p_five_or_more = 0.55  # over 4.5
    fitted = fit_line(POISSON, 4.5, p_five_or_more)
    exactly_five = stats.poisson.pmf(5, fitted.location)
    assert shift_probability(POISSON, p_five_or_more, 4.5, 5.5) == pytest.approx(
        p_five_or_more - exactly_five
    )


@pytest.mark.parametrize("model", YARDAGE, ids=MODEL_IDS[:4])
@given(st.floats(30, 250), probabilities)
@PROPERTY
def test_the_yardage_slope_is_the_density(model: StatModel, line: float, p: float) -> None:
    fitted = fit_line(model, line, p)
    h = line * 1e-5
    numeric = (fitted.p_over(line - h) - fitted.p_over(line + h)) / (2 * h)
    assert line_slope(model, p, line) == pytest.approx(numeric, rel=1e-4)


@pytest.mark.parametrize("model", COUNTS, ids=MODEL_IDS[4:])
def test_the_count_slope_is_the_probability_of_the_value_between_the_half_lines(
    model: StatModel,
) -> None:
    fitted = fit_line(model, 4.5, 0.55)
    assert line_slope(model, 0.55, 4.5) == pytest.approx(fitted.sf(4.0) - fitted.sf(5.0))
    assert line_slope(model, 0.55, 4.5) > 0


def test_the_consensus_view_delegates_to_the_fit() -> None:
    view = FittedLineModel(GAMMA)
    assert view.shift(0.5, 64.5, 65.5) == shift_probability(GAMMA, 0.5, 64.5, 65.5)
    assert view.slope(0.5, 64.5) == line_slope(GAMMA, 0.5, 64.5)


def test_vectorized_quantiles_match_the_scalar_ones() -> None:
    import numpy as np

    probabilities = np.array([0.01, 0.2, 0.5, 0.8, 0.99])
    for model, line in (
        (StatModel("gamma", 3.0, ZeroInflation((10.0, 40.0), (0.15, 0.02))), 12.5),
        (StatModel("lognormal", 0.6), 50.5),
        (StatModel("negbin", 16.0), 14.5),
        (StatModel("poisson"), 4.5),
    ):
        fitted = fit_line(model, line, 0.5)
        assert list(fitted.quantiles(probabilities)) == pytest.approx(
            [fitted.quantile(q) for q in probabilities]
        )
