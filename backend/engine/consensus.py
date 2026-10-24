"""Combine the books' prices for one player market into one fair line and one probability. Pure.

Given every book's latest quote for a market as of some moment, `consensus` returns the fair
line, the fair probability of the over at that line, how many books it used, and how much they
disagree. In order:

1. Keep each book's latest quote taken no later than `as_of`, and drop quotes older than
   `stale_after` before the reference moment (`as_of`, or the freshest quote if none is given).
2. De-vig each book (`engine.odds`). A book that posts only one side is de-vigged with an assumed
   margin (`devig_one_way`), or dropped if `one_sided_overround` is None.
3. If the books post different lines, pick a reference line, the weighted median of the lines
   posted, and shift every book's probability to it.
4. Average the probabilities with weight = sharpness weight x recency decay.
5. Measure disagreement: how far each book's line would have to move, in the market's own units
   (yards, receptions), to match the consensus, as a weighted standard deviation. For a market
   without a line (anytime TD) it is the weighted standard deviation of the probabilities.

TODO(/p3-distributions): step 3 uses a stand-in, a normal curve whose spread is a fixed fraction
of the line (`STUB_LINE_CV`). Replace it with the fitted distribution's slope once that exists.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import NormalDist
from typing import Final, Literal

from engine.odds import (
    InvalidOddsError,
    Method,
    american_to_implied_prob,
    devig_one_way,
    devig_two_way,
)

_NORMAL: Final = NormalDist()

# How spread out a stat is, as a fraction of its line: sigma = cv x line. Rough stand-ins for the
# fitted distributions (TODO(/p3-distributions)); they only decide how far a half-point or a
# point of difference between books moves a probability. Keyed by `markets.stat`.
STUB_LINE_CV: Final[Mapping[str, float]] = {
    "pass_yds": 0.25,
    "pass_tds": 0.65,
    "pass_completions": 0.22,
    "interceptions": 1.2,
    "rush_yds": 0.7,
    "rush_attempts": 0.35,
    "receptions": 0.5,
    "rec_yds": 0.75,
}
DEFAULT_LINE_CV: Final = 0.5
_MIN_LINE: Final = 0.5  # spread is never computed from a line below this
# Shifted probabilities stay at least this far from 0 and 1: an absurd gap between two books'
# lines must not turn into a certainty (the database requires 0 < fair_p_over < 1).
_EDGE: Final = 1e-6
_MIN_SLOPE: Final = 1e-12


@dataclass(frozen=True)
class BookQuote:
    """One book's latest quote for a market. `under_price` is None when only one side is posted,
    and `line` is None for markets that don't have one (anytime TD)."""

    book: str
    line: float | None
    over_price: int
    under_price: int | None
    captured_at: datetime


@dataclass(frozen=True)
class ConsensusConfig:
    method: Method = "multiplicative"
    stale_after: timedelta = timedelta(minutes=90)
    half_life: timedelta = timedelta(minutes=45)
    one_sided_overround: float | None = 0.10
    line_cv: float = DEFAULT_LINE_CV
    min_books: int = 1


@dataclass(frozen=True)
class Consensus:
    fair_line: float | None
    fair_p_over: float
    books_count: int
    dispersion: float | None
    """None with a single book. In line units, or probability points if `fair_line` is None."""
    dispersion_unit: Literal["line", "probability"]
    books: tuple[str, ...]
    one_sided_books: int = 0
    dropped_stale: tuple[str, ...] = field(default=())
    dropped_invalid: tuple[str, ...] = field(default=())


def recency_weight(age: timedelta, half_life: timedelta) -> float:
    """1 for a fresh quote, 0.5 after one half-life, 0.25 after two."""
    return float(0.5 ** (max(age.total_seconds(), 0.0) / half_life.total_seconds()))


def shift_probability(p_over: float, from_line: float, to_line: float, cv: float) -> float:
    """P(over `to_line`) for a book whose price says P(over `from_line`) = `p_over`.

    The stand-in model: the stat is normal with spread `cv` x `to_line`, so moving the line up
    lowers the probability and moving it down raises it. The result is kept within
    [1e-6, 1 - 1e-6].
    """
    if from_line == to_line:
        return p_over
    sigma = cv * max(to_line, _MIN_LINE)
    shifted = _NORMAL.cdf(_NORMAL.inv_cdf(p_over) - (to_line - from_line) / sigma)
    return min(max(shifted, _EDGE), 1 - _EDGE)


def weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    """The value where half the weight lies at or below it. On an exact tie, the lower value."""
    ordered = sorted(zip(values, weights, strict=True))
    half = sum(weights) / 2
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= half:
            return value
    return ordered[-1][0]


def consensus(
    quotes: Iterable[BookQuote],
    weights: Mapping[str, float] | None = None,
    config: ConsensusConfig = ConsensusConfig(),  # noqa: B008  (frozen, so safe to share)
    as_of: datetime | None = None,
) -> Consensus | None:
    """The consensus for one player market, or None if no usable book remains.

    `weights` maps a book to its sharpness weight (default 1; 0 or less mutes a book). Quotes
    taken after `as_of` are ignored, so the result never uses information from the future.
    """
    weights = weights or {}
    latest: dict[str, BookQuote] = {}
    for quote in sorted(quotes, key=lambda q: q.captured_at):
        if as_of is None or quote.captured_at <= as_of:
            latest[quote.book] = quote
    if not latest:
        return None

    reference = as_of or max(q.captured_at for q in latest.values())
    live = [q for q in latest.values() if reference - q.captured_at <= config.stale_after]
    stale = sorted(q.book for q in latest.values() if q not in live)

    lined = any(q.line is not None for q in live)
    fair: list[tuple[BookQuote, float, float]] = []  # quote, fair P(over), weight
    invalid: list[str] = []
    one_sided = 0
    for quote in live:
        weight = weights.get(quote.book, 1.0)
        if weight <= 0:
            continue
        probability = _fair_probability(quote, config)
        if probability is None or (lined and quote.line is None):
            invalid.append(quote.book)
            continue
        one_sided += quote.under_price is None
        weight *= recency_weight(reference - quote.captured_at, config.half_life)
        fair.append((quote, probability, weight))

    if len(fair) < max(config.min_books, 1):
        return None

    total = sum(weight for _, _, weight in fair)
    probabilities = [p for _, p, _ in fair]
    fair_weights = [w for _, _, w in fair]
    fair_line: float | None = None
    slope = 1.0
    if lined:
        lines = [q.line for q, _, _ in fair if q.line is not None]
        fair_line = weighted_median(lines, fair_weights)
        probabilities = [
            shift_probability(p, q.line, fair_line, config.line_cv)
            for q, p, _ in fair
            if q.line is not None
        ]
    fair_p_over = sum(w * p for w, p in zip(fair_weights, probabilities, strict=True)) / total
    if lined and fair_line is not None:
        sigma = config.line_cv * max(fair_line, _MIN_LINE)
        slope = max(_NORMAL.pdf(_NORMAL.inv_cdf(fair_p_over)) / sigma, _MIN_SLOPE)

    dispersion: float | None = None
    if len(fair) > 1:
        # A book's line distance from the consensus: how far its line would have to move for its
        # probability to equal the consensus one. Zero-mean by construction, so no centring.
        gaps = [(p - fair_p_over) / slope for p in probabilities]
        dispersion = math.sqrt(
            sum(w * g * g for w, g in zip(fair_weights, gaps, strict=True)) / total
        )

    return Consensus(
        fair_line=fair_line,
        fair_p_over=fair_p_over,
        books_count=len(fair),
        dispersion=dispersion,
        dispersion_unit="line" if lined else "probability",
        books=tuple(sorted(q.book for q, _, _ in fair)),
        one_sided_books=one_sided,
        dropped_stale=tuple(stale),
        dropped_invalid=tuple(sorted(invalid)),
    )


def _fair_probability(quote: BookQuote, config: ConsensusConfig) -> float | None:
    """The book's de-vigged P(over), or None if its prices can't be used."""
    try:
        over = american_to_implied_prob(quote.over_price)
        if quote.under_price is None:
            if config.one_sided_overround is None:
                return None
            return devig_one_way(over, config.one_sided_overround, config.method)
        under = american_to_implied_prob(quote.under_price)
        return devig_two_way(over, under, config.method).over
    except InvalidOddsError:
        return None
