"""Experimental market-fitted QB distributions. Pure functions; no I/O.

Ranges are conditional on a normal workload, not injury-adjusted or calibrated.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import NormalDist, mean


@dataclass(frozen=True)
class Observation:
    book: str
    line: float
    probability: float
    method: str


@dataclass(frozen=True)
class Distribution:
    family: str
    location: float
    shape: float = 0.0
    fit_rmse: float = 0.0

    @property
    def expected(self) -> float:
        if self.family == "lognormal":
            return math.exp(self.location + self.shape**2 / 2)
        return self.location

    def sample(self, rng: random.Random) -> float:
        if self.family == "lognormal":
            return rng.lognormvariate(self.location, self.shape)
        product, count = 1.0, 0
        cutoff = math.exp(-self.location)
        while product > cutoff:
            product *= rng.random()
            count += 1
        return float(max(0, count - 1))


def implied_probability(odds: float) -> float:
    if not math.isfinite(odds) or abs(odds) < 100:
        raise ValueError("Expected finite American odds with magnitude >= 100")
    return 100 / (odds + 100) if odds > 0 else -odds / (100 - odds)


def fair_over(over: float, under: float) -> float:
    p, q = implied_probability(over), implied_probability(under)
    return p / (p + q)


def weights(observations: list[Observation]) -> list[float]:
    """Give every book equal total influence, regardless of ladder length."""
    counts = {o.book: sum(p.book == o.book for p in observations) for o in observations}
    return [1 / counts[o.book] for o in observations]


def fit_yards(observations: list[Observation]) -> Distribution:
    valid = [o for o in observations if o.line > 0 and 0.03 < o.probability < 0.97]
    if len({o.line for o in valid}) < 3:
        raise ValueError("Need at least three distinct usable yardage thresholds")
    if max(o.probability for o in valid) - min(o.probability for o in valid) < 0.2:
        raise ValueError("Yardage ladder has insufficient probability spread")
    w = weights(valid)
    z = [NormalDist().inv_cdf(1 - o.probability) for o in valid]
    y = [math.log(o.line) for o in valid]
    total = sum(w)
    mz, my = (
        sum(a * b for a, b in zip(z, w, strict=True)) / total,
        sum(a * b for a, b in zip(y, w, strict=True)) / total,
    )
    denominator = sum(v * (x - mz) ** 2 for x, v in zip(z, w, strict=True))
    if denominator < 1e-8:
        raise ValueError("Unidentifiable yardage dispersion")
    sigma = sum(v * (x - mz) * (t - my) for x, t, v in zip(z, y, w, strict=True)) / denominator
    if not 0.05 <= sigma <= 2:
        raise ValueError("Yardage ladder implies an implausible or nonmonotone fit")
    mu = my - sigma * mz
    rmse = math.sqrt(
        sum(
            v * (1 - NormalDist(mu, sigma).cdf(math.log(o.line)) - o.probability) ** 2
            for o, v in zip(valid, w, strict=True)
        )
        / total
    )
    if rmse > 0.15:
        raise ValueError("Yardage fit exceeds probability-error tolerance")
    return Distribution("lognormal", mu, sigma, rmse)


def poisson_over(rate: float, line: float) -> float:
    if line < 0:
        return 1.0
    term = math.exp(-rate)
    cumulative = term
    for k in range(1, math.floor(line) + 1):
        term *= rate / k
        cumulative += term
    return max(0.0, min(1.0, 1 - cumulative))


def fit_count(observations: list[Observation]) -> Distribution:
    valid = [o for o in observations if o.line >= 0 and 0 < o.probability < 1]
    if not valid:
        raise ValueError("No usable count probabilities")
    # Invert each threshold separately, then average rates within and across books.
    # Avoid an optimizer that assumes a multi-threshold error surface is unimodal.
    by_book: dict[str, list[float]] = {}
    for o in valid:
        low, high = 0.0, 30.0
        if poisson_over(high, o.line) < o.probability:
            raise ValueError("Count rate exceeds supported range")
        for _ in range(70):
            mid = (low + high) / 2
            if poisson_over(mid, o.line) < o.probability:
                low = mid
            else:
                high = mid
        by_book.setdefault(o.book, []).append((low + high) / 2)
    rate = mean(mean(values) for values in by_book.values())
    w = weights(valid)
    rmse = math.sqrt(
        sum(
            v * (poisson_over(rate, o.line) - o.probability) ** 2
            for o, v in zip(valid, w, strict=True)
        )
        / sum(w)
    )
    if rmse > 0.15:
        raise ValueError("Poisson fit exceeds probability-error tolerance")
    return Distribution("poisson", rate, fit_rmse=rmse)


def simulate(
    distributions: dict[str, Distribution],
    scoring: dict[str, float],
    draws: int,
    seed: int,
) -> list[float]:
    if draws < 1000:
        raise ValueError("Use at least 1,000 draws")
    if set(distributions) != set(scoring):
        raise ValueError("Every scored stat must have a distribution; missing is not zero")
    if not all(math.isfinite(v) for v in scoring.values()):
        raise ValueError("Scoring weights must be finite")
    rng = random.Random(seed)  # noqa: S311 - reproducible simulation, not cryptography
    return [sum(d.sample(rng) * scoring[k] for k, d in distributions.items()) for _ in range(draws)]


def summarize(samples: list[float], expected: float) -> dict[str, float]:
    ordered = sorted(samples)
    average = mean(samples)
    result = {
        "mean": expected,
        "standard_deviation": math.sqrt(mean((x - average) ** 2 for x in samples)),
    }
    for p in (10, 20, 50, 80, 90):
        result[f"p{p}"] = ordered[round((len(ordered) - 1) * p / 100)]
    return {k: round(v, 2) for k, v in result.items()}


def compare(samples: list[float], reference: list[float]) -> dict[str, float]:
    if len(samples) != len(reference) or not samples:
        raise ValueError("Comparisons require equal nonempty simulations")
    wins = mean(a > b for a, b in zip(samples, reference, strict=True))
    ties = mean(a == b for a, b in zip(samples, reference, strict=True))
    return {
        "probability_outscores_reference": round(wins, 4),
        "probability_ties_reference": round(ties, 4),
    }
