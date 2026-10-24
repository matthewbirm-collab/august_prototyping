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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import numpy as np
from numpy.typing import NDArray
from scipy import special

from engine.distributions import FittedDistribution
from engine.scoring import Position, ScoringWeights

ENGINE_VERSION: Final = "3"
Source = Literal["market", "model"]
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
# Correlations between a player's stats in one game, on the normal-score scale, by position.
# Measured on the 2023 tuning season by scripts/tune_gapfill.py; pairs not listed are 0.
CORRELATIONS: Final[dict[Position, dict[tuple[str, str], float]]] = {
    "QB": {
        ("pass_yds", "pass_tds"): 0.43,
        ("pass_yds", "interceptions"): 0.16,
        ("pass_yds", "rush_yds"): 0.18,
        ("rush_yds", "anytime_td"): 0.22,
        ("pass_tds", "anytime_td"): -0.11,
        ("pass_tds", "rush_yds"): 0.08,
    },
    "RB": {
        ("rush_yds", "anytime_td"): 0.30,
        ("rec_yds", "receptions"): 0.80,
        ("rec_yds", "anytime_td"): 0.16,
        ("receptions", "anytime_td"): 0.10,
        ("rush_yds", "rec_yds"): 0.06,
    },
    "WR": {
        ("rec_yds", "receptions"): 0.79,
        ("rec_yds", "anytime_td"): 0.32,
        ("receptions", "anytime_td"): 0.27,
    },
    "TE": {
        ("rec_yds", "receptions"): 0.81,
        ("rec_yds", "anytime_td"): 0.25,
        ("receptions", "anytime_td"): 0.25,
    },
}
MODEL_VOLUME_CV: Final = 0.4
"""Spread of the shared volume multiplier on gap-filled stats, fitted on 2023 so p20-p80
holds about 60% of outcomes."""
MODEL_CREDIT: Final = 0.4
"""How much a gap-filled stat counts toward the missing-markets part of confidence."""
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
    source: Source = "market"
    """"model" for a stat gap-filled from team volume and usage (`engine/gapfill.py`)."""


@dataclass(frozen=True)
class StatBreakdown:
    mean: float
    p20: float
    p80: float
    weight: float
    points: float
    source: str
    """"market" for a fitted prop, "model" for a gap-filled stat, "history" for the fumble
    rate."""
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
    """Stats with neither a market nor a fill."""
    modelled: tuple[str, ...] = ()
    """Stats filled by the model, counted at `MODEL_CREDIT` of a market."""


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
    correlations: Mapping[Position, Mapping[tuple[str, str], float]] | None = None,
    volume_cv: float | None = None,
) -> FantasyProjection:
    """Expected fantasy points, the p20-p80 range, a per-stat breakdown and a confidence score.

    The range draws a player's stats together (a Gaussian copula with `correlations`, by
    default `CORRELATIONS`): a receiver's yards, catches and touchdowns rise and fall as one.
    Gap-filled stats also share one volume multiplier with mean 1 and spread `volume_cv` (by
    default `MODEL_VOLUME_CV`), for the uncertainty in the player's share itself. Neither
    changes the mean.
    """
    if fumbles_lost_per_game < 0:
        raise ValueError("a fumble rate can't be negative")
    if stale_minutes <= 0 or samples < 100:
        raise ValueError("stale_minutes must be positive and samples at least 100")
    pay = weights.for_position(position)
    rng = np.random.default_rng(SEED)
    cv = MODEL_VOLUME_CV if volume_cv is None else volume_cv
    if cv < 0:
        raise ValueError("volume_cv can't be negative")

    total_mean = 0.0
    total_draws: NDArray[np.float64] = np.zeros(samples)
    breakdown: dict[str, StatBreakdown] = {}

    present = [stat for stat in POSITION_STATS[position] if stat in stats]
    uniforms = _correlated_uniforms(
        present, (correlations or CORRELATIONS).get(position, {}), samples, rng
    )
    volume = rng.gamma(1 / cv**2, cv**2, samples) if cv > 0 else np.ones(samples)

    for i, stat in enumerate(present):
        given = stats[stat]
        weight = pay[WEIGHT_KEY[stat]]
        dist = given.distribution
        mean = dist.mean
        draws = dist.quantiles(uniforms[:, i])
        if given.source == "model":
            draws = draws * volume
        breakdown[stat] = StatBreakdown(
            mean=mean,
            p20=dist.quantile(0.2),
            p80=dist.quantile(0.8),
            weight=weight,
            points=weight * mean,
            source=given.source,
            books=given.books if given.source == "market" else None,
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


def _correlated_uniforms(
    stats: Sequence[str],
    pairs: Mapping[tuple[str, str], float],
    samples: int,
    rng: np.random.Generator,
) -> NDArray[np.float64]:
    """`samples` x len(stats) uniforms whose normal scores have the listed correlations."""
    k = len(stats)
    matrix = np.eye(k)
    for i, a in enumerate(stats):
        for j, b in enumerate(stats):
            if i < j:
                rho = pairs.get((a, b), pairs.get((b, a), 0.0))
                matrix[i, j] = matrix[j, i] = rho
    # Clip to the nearest valid correlation matrix, in case measured pairs don't fit together.
    values, vectors = np.linalg.eigh(matrix)
    matrix = vectors @ np.diag(np.clip(values, 1e-6, None)) @ vectors.T
    scale = np.sqrt(np.diag(matrix))
    matrix = matrix / np.outer(scale, scale)
    normals = rng.standard_normal((samples, k)) @ np.linalg.cholesky(matrix).T
    return np.asarray(special.ndtr(normals), dtype=np.float64)


def confidence(
    position: Position,
    pay: Mapping[str, float],
    stats: Mapping[str, StatInput],
    stale_minutes: float,
) -> Confidence:
    """Product of four penalties, each weighted by how many points the stat is worth.

    A stat's importance is its scoring weight times a typical game total, so a missing yardage
    market costs more than a missing interception market, and a scoring config that pays nothing
    for a stat ignores its data quality. A gap-filled stat covers `MODEL_CREDIT` of its
    importance; disagreement, book count and freshness are judged on the market stats only.
    """
    importance = {
        stat: abs(pay[WEIGHT_KEY[stat]]) * TYPICAL_MEANS[position][stat]
        for stat in POSITION_STATS[position]
    }
    total = sum(importance.values())
    present = {
        stat: stats[stat] for stat in importance if stat in stats and stats[stat].source == "market"
    }
    modelled = tuple(sorted(s for s in importance if s in stats and stats[s].source == "model"))
    missing = tuple(sorted(stat for stat in importance if stat not in stats))
    if total == 0:
        return Confidence(1.0, 1.0, 1.0, 1.0, 1.0, missing, modelled)

    covered = sum(importance[s] for s in present)
    credited = covered + MODEL_CREDIT * sum(importance[s] for s in modelled)
    if credited == 0:
        return Confidence(0.0, 0.0, 1.0, 1.0, 1.0, missing, modelled)

    def average(part: dict[str, float]) -> float:
        if covered == 0:
            return 1.0
        return sum(importance[s] * part[s] for s in present) / covered

    missing_factor = credited / total
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
        modelled=modelled,
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
