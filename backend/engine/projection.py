"""Assemble a fantasy projection from a player's fitted stat distributions. Pure.

    expected points = sum over stats of (scoring weight x stat MEAN)

The mean, not the line: lines sit near the median and yardage is right-skewed. Weights come from
`ScoringWeights` (the `scoring_configs` table), never from this module. Touchdowns follow
CLAUDE.md rule 2:

    RB / WR / TE   total TDs come from the anytime-TD distribution only
    QB             passing TDs from the pass-TD prop, rushing scores from anytime TD

Fumbles lost have no market, so they come in as the player's trailing rate per game.

The p20/p80 range sums the stats' distributions as if independent (PLAN §4.7) by simulation with a
fixed seed, so the same inputs always give the same range. Confidence in [0, 1] multiplies four
penalties: missing markets, books disagreeing, few books, and old quotes.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import numpy as np
from numpy.typing import NDArray

from engine.distributions import FittedDistribution
from engine.scoring import Position, ScoringWeights

ENGINE_VERSION: Final = "1"
SAMPLES: Final = 20_000
SEED: Final = 20_260_101

# Which scoring weight each market stat is paid at. Anytime TD is paid at the rushing/receiving
# touchdown weight; a QB's passing touchdowns are the separate `pass_tds` stat.
WEIGHT_KEY: Final = {
    "pass_yds": "pass_yds",
    "pass_tds": "pass_td",
    "interceptions": "interception",
    "rush_yds": "rush_yds",
    "rec_yds": "rec_yds",
    "receptions": "reception",
    "anytime_td": "td",
}

# The stats that make up a player's score at each position. A market outside this list (a
# receiver's rushing yards) is not used, so touchdowns can't be counted twice.
POSITION_STATS: Final[dict[Position, tuple[str, ...]]] = {
    "QB": ("pass_yds", "pass_tds", "interceptions", "rush_yds", "anytime_td"),
    "RB": ("rush_yds", "rec_yds", "receptions", "anytime_td"),
    "WR": ("rec_yds", "receptions", "anytime_td"),
    "TE": ("rec_yds", "receptions", "anytime_td"),
}

# Rough typical game lines, used only to judge how much of a score a missing market would have
# carried (confidence). They never enter the projection itself.
TYPICAL_MEANS: Final[dict[Position, dict[str, float]]] = {
    "QB": {
        "pass_yds": 250.0,
        "pass_tds": 1.6,
        "interceptions": 0.7,
        "rush_yds": 20.0,
        "anytime_td": 0.15,
    },
    "RB": {"rush_yds": 55.0, "rec_yds": 20.0, "receptions": 2.5, "anytime_td": 0.5},
    "WR": {"rec_yds": 55.0, "receptions": 4.5, "anytime_td": 0.45},
    "TE": {"rec_yds": 40.0, "receptions": 3.5, "anytime_td": 0.35},
}

# How far apart books may be before confidence halves: a fraction of the line, or probability
# points for lineless markets.
DISPERSION_SCALE_LINE: Final = 0.10
DISPERSION_SCALE_PROBABILITY: Final = 0.05
FULL_BOOKS: Final = 3
STALE_FLOOR: Final = 0.5
"""Confidence multiplier for a quote as old as the stale limit."""


@dataclass(frozen=True)
class StatInput:
    """One stat's fitted distribution and how good the data behind it was."""

    distribution: FittedDistribution
    books: int
    dispersion: float | None = None
    """Consensus dispersion: in line units, or probability points when `line` is None."""
    line: float | None = None
    """The consensus line, to scale a line-unit dispersion; None for anytime TD."""
    age_minutes: float = 0.0
    """Age of the newest quote behind it, at `as_of`."""


@dataclass(frozen=True)
class StatBreakdown:
    mean: float
    p20: float
    p80: float
    weight: float
    points: float
    source: str
    """"market" for a fitted prop, "history" for the fumble rate."""
    books: int | None = None


@dataclass(frozen=True)
class Confidence:
    score: float
    missing_markets: float
    dispersion: float
    books: float
    freshness: float
    """Each part is a multiplier in (0, 1]; the score is their product."""
    missing: tuple[str, ...]


@dataclass(frozen=True)
class FantasyProjection:
    mean: float
    p20: float
    p80: float
    breakdown: dict[str, StatBreakdown]
    confidence: Confidence


def project(
    position: Position,
    weights: ScoringWeights,
    stats: Mapping[str, StatInput],
    fumbles_lost_per_game: float,
    *,
    stale_minutes: float = 90.0,
    samples: int = SAMPLES,
) -> FantasyProjection:
    """Expected fantasy points, the p20-p80 range, a per-stat breakdown and a confidence score."""
    if fumbles_lost_per_game < 0:
        raise ValueError("a fumble rate can't be negative")
    if stale_minutes <= 0 or samples < 100:
        raise ValueError("stale_minutes must be positive and samples at least 100")
    pay = weights.for_position(position)
    rng = np.random.default_rng(SEED)

    total_mean = 0.0
    total_draws: NDArray[np.float64] = np.zeros(samples)
    breakdown: dict[str, StatBreakdown] = {}

    for stat in POSITION_STATS[position]:
        given = stats.get(stat)
        if given is None:
            continue
        weight = pay[WEIGHT_KEY[stat]]
        dist = given.distribution
        mean = dist.mean
        draws = dist.sample(samples, rng)
        breakdown[stat] = StatBreakdown(
            mean=mean,
            p20=dist.quantile(0.2),
            p80=dist.quantile(0.8),
            weight=weight,
            points=weight * mean,
            source="market",
            books=given.books,
        )
        total_mean += weight * mean
        total_draws += weight * draws

    fumble_weight = pay["fumble_lost"]
    fumble_draws = rng.poisson(fumbles_lost_per_game, samples).astype(np.float64)
    breakdown["fumbles_lost"] = StatBreakdown(
        mean=fumbles_lost_per_game,
        p20=float(np.quantile(fumble_draws, 0.2)),
        p80=float(np.quantile(fumble_draws, 0.8)),
        weight=fumble_weight,
        points=fumble_weight * fumbles_lost_per_game,
        source="history",
    )
    total_mean += fumble_weight * fumbles_lost_per_game
    total_draws += fumble_weight * fumble_draws

    return FantasyProjection(
        mean=total_mean,
        p20=float(np.quantile(total_draws, 0.2)),
        p80=float(np.quantile(total_draws, 0.8)),
        breakdown=breakdown,
        confidence=confidence(position, pay, stats, stale_minutes),
    )


def confidence(
    position: Position,
    pay: Mapping[str, float],
    stats: Mapping[str, StatInput],
    stale_minutes: float,
) -> Confidence:
    """Product of four penalties, each weighted by how many points the stat is worth.

    A stat's importance is its scoring weight times a typical game total, so a missing yardage
    market costs more than a missing interception market, and a scoring config that pays nothing
    for a stat ignores its data quality.
    """
    importance = {
        stat: abs(pay[WEIGHT_KEY[stat]]) * TYPICAL_MEANS[position][stat]
        for stat in POSITION_STATS[position]
    }
    total = sum(importance.values())
    present = {stat: stats[stat] for stat in importance if stat in stats}
    missing = tuple(sorted(stat for stat in importance if stat not in stats))
    if total == 0:
        return Confidence(1.0, 1.0, 1.0, 1.0, 1.0, missing)

    covered = sum(importance[s] for s in present)
    if covered == 0:
        return Confidence(0.0, 0.0, 1.0, 1.0, 1.0, missing)

    def average(part: dict[str, float]) -> float:
        return sum(importance[s] * part[s] for s in present) / covered

    missing_factor = covered / total
    dispersion_factor = average({s: _dispersion_factor(i) for s, i in present.items()})
    books_factor = average({s: _books_factor(i.books) for s, i in present.items()})
    fresh_factor = average({s: _freshness_factor(i, stale_minutes) for s, i in present.items()})
    return Confidence(
        score=missing_factor * dispersion_factor * books_factor * fresh_factor,
        missing_markets=missing_factor,
        dispersion=dispersion_factor,
        books=books_factor,
        freshness=fresh_factor,
        missing=missing,
    )


def _dispersion_factor(given: StatInput) -> float:
    """1 when books agree; a half at a disagreement of one scale; unknown (one book) counts as
    agreeing, since the few-books penalty already covers it."""
    if given.dispersion is None:
        return 1.0
    if given.line is None:
        relative = given.dispersion / DISPERSION_SCALE_PROBABILITY
    else:
        relative = given.dispersion / max(abs(given.line), 1e-9) / DISPERSION_SCALE_LINE
    return 1.0 / (1.0 + relative * relative)


def _books_factor(books: int) -> float:
    """0.5 with one book, 0.75 with two, 1.0 from three."""
    return (1 + min(max(books, 0), FULL_BOOKS)) / (1 + FULL_BOOKS)


def _freshness_factor(given: StatInput, stale_minutes: float) -> float:
    age = min(max(given.age_minutes, 0.0), stale_minutes)
    return 1.0 - (1.0 - STALE_FLOOR) * age / stale_minutes
