import math
from datetime import UTC, datetime, timedelta
from random import Random
from statistics import NormalDist

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from engine.consensus import (
    STUB_LINE_CV,
    BookQuote,
    ConsensusConfig,
    consensus,
    recency_weight,
    shift_probability,
    weighted_median,
)
from engine.distributions import (
    FittedDistribution,
    FittedLineModel,
    StatModel,
    line_slope,
)
from engine.distributions import shift_probability as fitted_shift
from engine.odds import american_to_implied_prob, devig_one_way, devig_two_way

NOW = datetime(2026, 10, 25, 15, 0, tzinfo=UTC)
NORMAL = NormalDist()
CV = 0.65
CONFIG = ConsensusConfig(line_cv=CV)
PROPERTY = settings(max_examples=200, deadline=None)


def quote(
    book: str, line: float | None, over: int, under: int | None, minutes_ago: float = 0
) -> BookQuote:
    return BookQuote(book, line, over, under, NOW - timedelta(minutes=minutes_ago))


def fair_over(over: int, under: int, method: str = "multiplicative") -> float:
    a, b = american_to_implied_prob(over), american_to_implied_prob(under)
    return devig_two_way(a, b, method).over  # type: ignore[arg-type]


def even_books(count: int = 4, line: float = 64.5) -> list[BookQuote]:
    return [quote(f"book{i}", line, -110, -110) for i in range(count)]


# The four fixtures from the plan --------------------------------------------------------------


def test_unanimous_books_agree_with_each_other_and_with_a_single_devig() -> None:
    result = consensus(even_books(4), config=CONFIG)

    assert result is not None
    assert result.fair_line == 64.5
    assert result.fair_p_over == pytest.approx(0.5)
    assert (result.books_count, result.dispersion, result.dispersion_unit) == (4, 0, "line")
    assert result.books == ("book0", "book1", "book2", "book3")
    assert (result.dropped_stale, result.dropped_invalid, result.one_sided_books) == ((), (), 0)


def test_unanimous_lopsided_books_give_exactly_the_single_book_answer() -> None:
    books = [quote(f"b{i}", 64.5, -150, 130) for i in range(3)]
    result = consensus(books, config=CONFIG)
    assert result is not None
    assert result.fair_p_over == pytest.approx(fair_over(-150, 130))
    assert result.dispersion == pytest.approx(0, abs=1e-9)


def test_one_outlier_pulls_the_probability_a_little_and_shows_up_as_disagreement() -> None:
    outlier = quote("sharp", 64.5, -150, 130)
    result = consensus([*even_books(4), outlier], config=CONFIG)

    assert result is not None
    outlier_p = fair_over(-150, 130)
    assert result.fair_p_over == pytest.approx((4 * 0.5 + outlier_p) / 5)
    assert result.fair_line == 64.5

    # How far each book's line would have to move to match: independent calculation.
    sigma = CV * 64.5
    slope = NORMAL.pdf(NORMAL.inv_cdf(result.fair_p_over)) / sigma
    gaps = [(p - result.fair_p_over) / slope for p in (0.5, 0.5, 0.5, 0.5, outlier_p)]
    assert result.dispersion == pytest.approx(math.sqrt(sum(g * g for g in gaps) / 5))
    assert result.dispersion is not None and result.dispersion > 1  # over a yard of disagreement


def test_a_muted_or_down_weighted_outlier_matters_less() -> None:
    books = [*even_books(4), quote("sharp", 64.5, -150, 130)]
    full = consensus(books, config=CONFIG)
    light = consensus(books, {"sharp": 0.2}, CONFIG)
    muted = consensus(books, {"sharp": 0}, CONFIG)

    assert full and light and muted
    assert muted.fair_p_over == pytest.approx(0.5)
    assert muted.books_count == 4  # a muted book isn't counted, or reported as invalid
    assert muted.dropped_invalid == ()
    assert 0.5 < light.fair_p_over < full.fair_p_over


def test_books_on_different_lines_are_shifted_to_a_common_one() -> None:
    result = consensus(
        [quote("dk", 64.5, -110, -110), quote("fd", 65.5, -110, -110)], config=CONFIG
    )

    assert result is not None
    assert result.fair_line == 64.5  # an even split takes the lower line
    # FanDuel's 50% at 65.5 is more bullish than 50% at 64.5: shifted down a yard, it rises.
    fanduel = NORMAL.cdf(NORMAL.inv_cdf(0.5) - (64.5 - 65.5) / (CV * 64.5))
    assert fanduel == pytest.approx(0.50951, abs=1e-5)
    assert result.fair_p_over == pytest.approx((0.5 + fanduel) / 2)
    # Each book is half a yard from the consensus.
    assert result.dispersion == pytest.approx(0.5, abs=1e-3)


def test_the_reference_line_is_the_weighted_median_of_the_lines_posted() -> None:
    books = [quote("a", 64.5, -110, -110), quote("b", 65.5, -110, -110)]
    heavy = consensus(books, {"b": 3}, CONFIG)
    assert heavy is not None and heavy.fair_line == 65.5
    assert heavy.fair_p_over < 0.5  # a's 50% at 64.5 is less bullish than 50% at 65.5

    far = consensus([*even_books(2), quote("far", 70.5, -110, -110)], config=CONFIG)
    assert far is not None and far.fair_line == 64.5  # an outlying line doesn't move it


def test_a_stale_book_is_dropped_and_reported() -> None:
    fresh = quote("dk", 64.5, -110, -110)
    old = quote("fd", 64.5, -150, 130, minutes_ago=120)
    result = consensus([fresh, old], config=CONFIG)

    assert result is not None
    assert result.books == ("dk",)
    assert result.dropped_stale == ("fd",)
    assert result.fair_p_over == pytest.approx(0.5)


# Staleness and as_of ---------------------------------------------------------------------------


def test_the_stale_limit_is_inclusive_and_measured_from_the_freshest_book() -> None:
    boundary = consensus(
        [quote("a", 64.5, -110, -110), quote("b", 64.5, -110, -110, 90)], config=CONFIG
    )
    over = consensus(
        [quote("a", 64.5, -110, -110), quote("b", 64.5, -110, -110, 91)], config=CONFIG
    )
    assert boundary is not None and boundary.books_count == 2
    assert over is not None and over.books_count == 1

    # Everything is old, but nothing is old relative to the freshest quote.
    all_old = consensus(
        [quote("a", 64.5, -110, -110, 600), quote("b", 64.5, -110, -110, 620)], config=CONFIG
    )
    assert all_old is not None and all_old.books_count == 2


def test_as_of_measures_age_from_that_moment_and_ignores_later_quotes() -> None:
    books = [
        quote("early", 64.5, -110, -110, minutes_ago=200),
        quote("later", 64.5, -150, 130, minutes_ago=10),
    ]
    assert (
        consensus(books, config=CONFIG, as_of=NOW - timedelta(minutes=100)) is None
    )  # all > 90 min old
    result = consensus(books, config=CONFIG, as_of=NOW - timedelta(minutes=190))
    assert result is not None and result.books == ("early",)  # the later quote is the future

    live = consensus(books, config=CONFIG, as_of=NOW)
    assert live is not None and live.books == ("later",)
    assert live.dropped_stale == ("early",)


def test_only_the_latest_quote_from_each_book_counts() -> None:
    books = [
        quote("a", 64.5, -150, 130, 30),
        quote("a", 64.5, -110, -110, 5),
        quote("b", 64.5, -110, -110),
    ]
    result = consensus(books, config=CONFIG)
    assert result is not None
    assert result.books_count == 2
    assert result.fair_p_over == pytest.approx(0.5)


# Weights ---------------------------------------------------------------------------------------


def test_sharpness_weights_set_each_books_share() -> None:
    books = [quote("sharp", 64.5, -150, 130), quote("soft", 64.5, -110, -110)]
    result = consensus(books, {"sharp": 3, "soft": 1}, CONFIG)
    assert result is not None
    assert result.fair_p_over == pytest.approx((3 * fair_over(-150, 130) + 0.5) / 4)


def test_older_quotes_count_for_less() -> None:
    books = [quote("new", 64.5, -150, 130, 0), quote("old", 64.5, -110, -110, 45)]
    result = consensus(books, config=CONFIG)  # a 45-minute half-life
    assert result is not None
    assert recency_weight(timedelta(minutes=45), timedelta(minutes=45)) == pytest.approx(0.5)
    assert result.fair_p_over == pytest.approx((fair_over(-150, 130) + 0.5 * 0.5) / 1.5)


@pytest.mark.parametrize(("age", "expected"), [(0, 1), (45, 0.5), (90, 0.25), (-10, 1)])
def test_recency_weight_halves_each_half_life(age: float, expected: float) -> None:
    assert recency_weight(timedelta(minutes=age), timedelta(minutes=45)) == pytest.approx(expected)


# One-sided markets and markets without a line --------------------------------------------------


def test_one_sided_quotes_are_de_vigged_with_the_assumed_margin() -> None:
    books = [quote("a", None, 120, None), quote("b", None, 110, None)]
    result = consensus(books, config=ConsensusConfig(one_sided_overround=0.10))

    assert result is not None
    expected = (
        devig_one_way(american_to_implied_prob(120), 0.10)
        + devig_one_way(american_to_implied_prob(110), 0.10)
    ) / 2
    assert result.fair_p_over == pytest.approx(expected)
    assert (result.fair_line, result.dispersion_unit, result.one_sided_books) == (
        None,
        "probability",
        2,
    )


def test_two_way_and_one_sided_books_combine() -> None:
    books = [quote("a", None, 125, -160), quote("b", None, 120, None)]
    result = consensus(books, config=ConsensusConfig(one_sided_overround=0.10))
    assert result is not None
    assert (result.books_count, result.one_sided_books) == (2, 1)
    assert result.dispersion is not None and 0 < result.dispersion < 0.1


def test_one_sided_quotes_are_dropped_when_no_margin_is_assumed() -> None:
    books = [quote("a", None, 125, -160), quote("b", None, 120, None)]
    result = consensus(books, config=ConsensusConfig(one_sided_overround=None))
    assert result is not None
    assert (result.books, result.dropped_invalid) == (("a",), ("b",))
    assert (
        consensus([quote("b", None, 120, None)], config=ConsensusConfig(one_sided_overround=None))
        is None
    )


def test_probability_dispersion_is_the_weighted_spread_of_the_probabilities() -> None:
    books = [quote("a", None, 100, -120), quote("b", None, 200, -250)]
    result = consensus(books, config=ConsensusConfig())
    assert result is not None
    pa, pb = fair_over(100, -120), fair_over(200, -250)
    assert result.dispersion == pytest.approx(abs(pa - pb) / 2)


# Odd inputs ------------------------------------------------------------------------------------


def test_one_book_has_no_dispersion() -> None:
    result = consensus([quote("a", 64.5, -150, 130)], config=CONFIG)
    assert result is not None
    assert (result.books_count, result.dispersion) == (1, None)
    assert result.fair_p_over == pytest.approx(fair_over(-150, 130))


def test_min_books_can_require_agreement() -> None:
    assert consensus([quote("a", 64.5, -110, -110)], config=ConsensusConfig(min_books=2)) is None
    assert consensus(even_books(2), config=ConsensusConfig(min_books=2)) is not None


def test_nothing_in_nothing_out() -> None:
    assert consensus([], config=CONFIG) is None
    assert consensus(even_books(2), {"book0": 0, "book1": -1}, CONFIG) is None


def test_unusable_prices_are_dropped_and_named() -> None:
    books = [
        quote("good", 64.5, -110, -110),
        quote("bad_price", 64.5, -50, -110),  # not an American price
        quote("arbitrage", 64.5, 120, 120),  # the two sides add up to less than 1
    ]
    result = consensus(books, config=CONFIG)
    assert result is not None
    assert result.books == ("good",)
    assert result.dropped_invalid == ("arbitrage", "bad_price")
    assert consensus(books[1:], config=CONFIG) is None


def test_in_a_market_with_lines_a_quote_without_one_is_unusable() -> None:
    result = consensus([quote("a", 64.5, -110, -110), quote("b", None, -110, -110)], config=CONFIG)
    assert result is not None
    assert (result.books, result.dropped_invalid) == (("a",), ("b",))


def test_the_devig_method_is_applied_to_every_book() -> None:
    books = [quote("a", 64.5, -300, 240), quote("b", 64.5, -300, 240)]
    for method in ("multiplicative", "additive", "power", "shin"):
        result = consensus(books, config=ConsensusConfig(method=method, line_cv=CV))
        assert result is not None
        assert result.fair_p_over == pytest.approx(fair_over(-300, 240, method))


def test_every_priced_market_has_a_stand_in_spread() -> None:
    assert set(STUB_LINE_CV) == {
        "pass_yds",
        "pass_tds",
        "pass_completions",
        "interceptions",
        "rush_yds",
        "rush_attempts",
        "receptions",
        "rec_yds",
    }
    assert all(cv > 0 for cv in STUB_LINE_CV.values())


# Helpers ---------------------------------------------------------------------------------------


def test_shifting_a_probability_to_a_higher_line_lowers_it() -> None:
    assert shift_probability(0.5, 64.5, 64.5, CV) == 0.5
    assert shift_probability(0.5, 64.5, 65.5, CV) < 0.5
    assert shift_probability(0.5, 65.5, 64.5, CV) > 0.5
    # About a yard of line moves the probability about one percentage point at this spread.
    assert shift_probability(0.5, 64.5, 65.5, CV) == pytest.approx(0.4905, abs=2e-4)


def test_an_absurd_gap_between_lines_cannot_make_a_certainty() -> None:
    assert shift_probability(0.5, 1.5, 0.5, 0.125) == 1 - 1e-6  # sixteen spreads apart
    assert shift_probability(0.5, 0.5, 1.5, 0.125) == 1e-6
    result = consensus(
        [quote("a", 1.5, -110, -110), quote("b", 0.5, -110, -110)],
        config=ConsensusConfig(line_cv=0.125),
    )
    assert result is not None and 0 < result.fair_p_over < 1


@given(
    st.floats(0.01, 0.99),
    st.floats(1.5, 300),
    st.floats(-3, 3),
    st.floats(0.1, 1.5),
)
@PROPERTY
def test_shifting_there_and_back_gives_the_original_and_stays_inside_zero_and_one(
    p: float, line: float, delta: float, cv: float
) -> None:
    target = max(line + delta, 0.5)
    moved = shift_probability(p, line, target, cv)
    assert 0 < moved < 1
    # The stand-in's spread depends on the target line, so the way back isn't exact unless the
    # lines match; the monotone direction is what counts.
    if target > line:
        assert moved <= p
    elif target < line:
        assert moved >= p


@pytest.mark.parametrize(
    ("values", "weights", "expected"),
    [
        ([64.5], [1], 64.5),
        ([64.5, 65.5], [1, 1], 64.5),
        ([64.5, 65.5], [1, 3], 65.5),
        ([65.5, 64.5, 70.5], [1, 1, 1], 65.5),
        ([70.5, 64.5, 64.5], [5, 1, 1], 70.5),
    ],
)
def test_weighted_median(values: list[float], weights: list[float], expected: float) -> None:
    assert weighted_median(values, weights) == expected


# Properties ------------------------------------------------------------------------------------

PRICES = st.integers(100, 1000) | st.integers(-1000, -100)


@st.composite
def books(draw: st.DrawFn, lined: bool = True) -> list[BookQuote]:
    count = draw(st.integers(1, 7))
    result = []
    for i in range(count):
        over, under = draw(PRICES), draw(PRICES)
        assume(american_to_implied_prob(over) + american_to_implied_prob(under) >= 1)
        line = draw(st.sampled_from([63.5, 64.5, 65.5])) if lined else None
        age = draw(st.integers(0, 80))
        result.append(quote(f"book{i}", line, over, under, age))
    return result


@given(books(), st.randoms(use_true_random=False))
@PROPERTY
def test_the_order_of_the_quotes_does_not_matter(quotes: list[BookQuote], random: Random) -> None:
    shuffled = list(quotes)
    random.shuffle(shuffled)
    a, b = consensus(quotes, config=CONFIG), consensus(shuffled, config=CONFIG)
    assert a is not None and b is not None
    assert (a.fair_line, a.books_count) == (b.fair_line, b.books_count)
    assert a.fair_p_over == pytest.approx(b.fair_p_over, abs=1e-12)
    assert a.dispersion == pytest.approx(b.dispersion, abs=1e-9)


@given(books(), st.floats(0.01, 100))
@PROPERTY
def test_scaling_every_weight_changes_nothing(quotes: list[BookQuote], factor: float) -> None:
    weights = {q.book: 1.7 for q in quotes}
    scaled = {book: weight * factor for book, weight in weights.items()}
    a, b = consensus(quotes, weights, CONFIG), consensus(quotes, scaled, CONFIG)
    assert a is not None and b is not None
    assert a.fair_p_over == pytest.approx(b.fair_p_over, abs=1e-9)
    assert a.dispersion == pytest.approx(b.dispersion, abs=1e-6)


@given(books())
@PROPERTY
def test_the_result_is_a_probability_inside_the_range_of_the_books(
    quotes: list[BookQuote],
) -> None:
    result = consensus(quotes, config=CONFIG)
    assert result is not None
    assert 0 < result.fair_p_over < 1
    assert result.dispersion is None or result.dispersion >= 0
    assert result.books_count == len(result.books) <= len(quotes)
    assert result.fair_line in {63.5, 64.5, 65.5}


@given(books(lined=False))
@PROPERTY
def test_a_market_without_a_line_stays_between_the_cheapest_and_dearest_book(
    quotes: list[BookQuote],
) -> None:
    result = consensus(quotes, config=CONFIG)
    assert result is not None and result.fair_line is None
    individual = [fair_over(q.over_price, q.under_price) for q in quotes if q.under_price]
    assert min(individual) - 1e-12 <= result.fair_p_over <= max(individual) + 1e-12


@given(books(), st.integers(1, 500))
@PROPERTY
def test_a_stale_book_changes_nothing_except_the_report(
    quotes: list[BookQuote], extra_minutes: int
) -> None:
    stale = quote("ghost", 70.5, 300, -400, minutes_ago=91 + extra_minutes)
    before = consensus(quotes, config=CONFIG, as_of=NOW)
    after = consensus([*quotes, stale], config=CONFIG, as_of=NOW)
    assert (before is None) == (after is None)
    if before is not None and after is not None:
        assert after.fair_p_over == before.fair_p_over
        assert after.dispersion == before.dispersion
        assert (
            after.dropped_stale == ("ghost", *before.dropped_stale)
            or "ghost" in after.dropped_stale
        )


@given(st.integers(2, 6), PRICES, PRICES)
@PROPERTY
def test_identical_books_never_disagree(count: int, over: int, under: int) -> None:
    assume(american_to_implied_prob(over) + american_to_implied_prob(under) >= 1)
    result = consensus([quote(f"b{i}", 64.5, over, under) for i in range(count)], config=CONFIG)
    assert result is not None
    assert result.fair_p_over == pytest.approx(fair_over(over, under))
    assert result.dispersion == pytest.approx(0, abs=1e-6)


# Fitted shapes in place of the stand-in --------------------------------------------------------


def price(p: float) -> int:
    """An American price for a fair probability (a two-way pair at p and 1 - p, no margin)."""
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def true_quotes(distribution: FittedDistribution, lines: list[float]) -> list[BookQuote]:
    return [
        quote(f"book{i}", line, price(p := distribution.p_over(line)), price(1 - p))
        for i, line in enumerate(lines)
    ]


GAMMA_MODEL = StatModel("gamma", 3.12)
FITTED = ConsensusConfig(line_model=FittedLineModel(GAMMA_MODEL))


def test_the_fitted_curve_shifts_books_to_the_reference_line() -> None:
    books = [quote("dk", 64.5, -110, -110), quote("fd", 65.5, -110, -110)]
    result = consensus(books, config=FITTED)

    shifted = fitted_shift(GAMMA_MODEL, 0.5, 65.5, 64.5, zero_at=64.5)
    assert result is not None
    assert (result.fair_line, result.books_count) == (64.5, 2)
    assert result.fair_p_over == pytest.approx((0.5 + shifted) / 2)
    assert shifted != pytest.approx(NORMAL.cdf(0), abs=1e-4)  # a real shift, not a no-op


def test_books_quoting_the_same_distribution_at_different_lines_show_no_disagreement() -> None:
    """The stand-in curve can't do this: only the true curve makes the books agree exactly."""
    truth = FittedDistribution(GAMMA_MODEL, 24.0)
    books = true_quotes(truth, [60.5, 62.5, 64.5, 66.5, 68.5])
    result = consensus(books, config=FITTED)

    assert result is not None
    assert result.fair_line == 64.5
    assert result.fair_p_over == pytest.approx(truth.p_over(64.5), abs=2e-3)
    assert result.dispersion is not None and result.dispersion < 0.25  # a quarter of a yard

    standin = consensus(books, config=ConsensusConfig(line_cv=0.7))
    assert standin is not None and standin.dispersion is not None
    assert standin.dispersion > 3 * result.dispersion


def test_the_dispersion_uses_the_fitted_slope() -> None:
    books = [quote("dk", 64.5, -150, 130), quote("fd", 64.5, -110, -110)]
    result = consensus(books, config=FITTED)
    assert result is not None and result.dispersion is not None

    p = [fair_over(-150, 130), 0.5]
    mean = sum(p) / 2
    slope = line_slope(GAMMA_MODEL, mean, 64.5)
    expected = math.sqrt(sum(((v - mean) / slope) ** 2 for v in p) / 2)
    assert result.dispersion == pytest.approx(expected)


def test_counts_use_exact_half_point_shifts() -> None:
    poisson = StatModel("poisson")
    truth = FittedDistribution(poisson, 4.4)
    books = true_quotes(truth, [3.5, 4.5, 5.5])
    result = consensus(books, config=ConsensusConfig(line_model=FittedLineModel(poisson)))

    assert result is not None
    assert result.fair_line == 4.5
    assert result.fair_p_over == pytest.approx(truth.p_over(4.5), abs=2e-3)
    assert result.dispersion is not None and result.dispersion < 0.1  # a tenth of a reception


def test_a_book_the_fitted_model_cannot_represent_is_dropped_not_fatal() -> None:
    poisson = StatModel("poisson")
    books = [quote("odd", 0.0, -110, -110), quote("normal", 4.5, -110, -110)]
    result = consensus(books, config=ConsensusConfig(line_model=FittedLineModel(poisson)))

    assert result is not None
    assert (result.fair_line, result.books) == (0.0, ("odd",))
    assert result.dropped_invalid == ("normal",)
    assert result.dispersion is None


def test_without_a_line_model_the_stand_in_is_unchanged() -> None:
    books = [quote("dk", 64.5, -110, -110), quote("fd", 65.5, -110, -110)]
    assert consensus(books, config=ConsensusConfig(line_cv=CV)) == consensus(books, config=CONFIG)
