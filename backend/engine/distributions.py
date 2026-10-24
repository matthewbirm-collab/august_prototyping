"""Turn the market's probability at a line into a distribution for the stat. Pure.

A consensus says "P(over 64.5 rushing yards) = 0.52". That is one point on a curve. Given a
family and a shape for the stat, one point fixes the curve, and the curve gives the mean, median
and the p20/p80 range that fantasy points are built from.

    gamma / lognormal   yardage: a continuous positive curve, plus an optional share of games at
                        exactly zero (`ZeroInflation`, which depends on the line)
    negbin / poisson    counts: receptions, completions, touchdowns... Half-point lines are
                        exact (over 4.5 means 5 or more); a whole-number line is a push for the
                        books, so the probability is conditional on no push
    bernoulli           anytime TD: the TD count is Poisson with lambda = -ln(1 - p)

The *shape* (gamma shape, log-normal sigma, negative-binomial r) comes from configuration, since
one point cannot pin it down. Every single-line fit is closed form. With several lines for one
player (alternate thresholds) `fit` fits location and shape jointly by least squares on the
probit scale, with a prior that keeps the shape near its configured value.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import numpy as np
from numpy.typing import NDArray
from scipy import optimize, special, stats

Family = Literal["gamma", "lognormal", "negbin", "poisson", "bernoulli"]
_CONTINUOUS: Final = frozenset({"gamma", "lognormal"})
_NEEDS_SHAPE: Final = frozenset({"gamma", "lognormal", "negbin"})

# Probabilities are kept this far from 0 and 1 so the inverse functions stay finite.
_EDGE: Final = 1e-6
_PROBIT_EDGE: Final = 1e-9
_LOCATION_BOUNDS: Final = (-25.0, 25.0)  # natural logs of a scale or a mean


@dataclass(frozen=True)
class ZeroInflation:
    """The share of games at exactly zero as a function of the line, interpolated in ln(line).

    Low-volume players have low lines and often finish with nothing: about 20% of receivers
    whose line is under 12 yards, 1% above 60. Outside the listed lines the end values hold.
    """

    lines: tuple[float, ...]
    shares: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.lines or len(self.lines) != len(self.shares):
            raise ValueError("zero inflation needs equal, non-empty lines and shares")
        if (
            any(a >= b for a, b in zip(self.lines, self.lines[1:], strict=False))
            or self.lines[0] <= 0
        ):
            raise ValueError("zero-inflation lines must be positive and strictly increasing")
        if any(not 0 <= s < 1 for s in self.shares):
            raise ValueError("zero-inflation shares must be in [0, 1)")

    def at(self, line: float) -> float:
        if line <= self.lines[0]:
            return self.shares[0]
        if line >= self.lines[-1]:
            return self.shares[-1]
        for (low, high), (s_low, s_high) in zip(
            zip(self.lines, self.lines[1:], strict=False),
            zip(self.shares, self.shares[1:], strict=False),
            strict=False,
        ):
            if low <= line <= high:
                weight = (math.log(line) - math.log(low)) / (math.log(high) - math.log(low))
                return s_low + weight * (s_high - s_low)
        return self.shares[-1]  # pragma: no cover


@dataclass(frozen=True)
class StatModel:
    """A family and its shape: gamma shape k, log-normal sigma, or negative-binomial r."""

    family: Family
    shape: float | None = None
    zero_inflation: ZeroInflation | None = None

    def __post_init__(self) -> None:
        if self.family in _NEEDS_SHAPE and not (self.shape and self.shape > 0):
            raise ValueError(f"{self.family} needs a positive shape")
        if self.family not in _NEEDS_SHAPE and self.shape is not None:
            raise ValueError(f"{self.family} has no shape")
        if self.zero_inflation is not None and self.family not in _CONTINUOUS:
            raise ValueError("only yardage families can be zero-inflated")

    @property
    def continuous(self) -> bool:
        return self.family in _CONTINUOUS


@dataclass(frozen=True)
class LinePoint:
    line: float
    p_over: float
    weight: float = 1.0


@dataclass(frozen=True)
class Summary:
    mean: float
    median: float
    p20: float
    p80: float


# Kernels: the probability above a line for a given location, and the inverse. ------------------


def _required(shape: float | None) -> float:
    if shape is None:
        raise ValueError("this family needs a shape")
    return shape


def _clamp(p: float, edge: float = _EDGE) -> float:
    return min(max(p, edge), 1 - edge)


def _is_whole(line: float) -> bool:
    return abs(line - round(line)) < 1e-9


def _tail_continuous(family: str, shape: float, location: float, x: float) -> float:
    """P(positive part > x). `location` is the gamma scale or the log-normal mu."""
    if family == "gamma":
        return float(special.gammaincc(shape, x / location))
    return float(special.ndtr((location - math.log(x)) / shape))


def _solve_continuous(family: str, shape: float, x: float, tail: float) -> float:
    if family == "gamma":
        return float(x / special.gammainccinv(shape, tail))
    return float(math.log(x) + shape * special.ndtri(tail))


def _count_at_least(family: str, shape: float | None, mean: float, k: int) -> float:
    """P(X >= k) for a count with the given mean."""
    if k <= 0:
        return 1.0
    if family == "negbin":
        r = _required(shape)
        return float(special.betainc(k, r, mean / (r + mean)))
    return float(special.gammainc(k, mean))


def _count_p_over(family: str, shape: float | None, mean: float, line: float) -> float:
    """What the books call P(over): above a half-point line, or above a whole-number line given
    no push."""
    if line == 0:
        raise ValueError("a count line of zero has no under: over zero is certain")
    above = _count_at_least(family, shape, mean, math.floor(line) + 1)
    if not _is_whole(line):
        return above
    at_least_line = _count_at_least(family, shape, mean, round(line))
    return above / (1 - (at_least_line - above))


def _solve_count(family: str, shape: float | None, line: float, p: float) -> float:
    k = math.floor(line) + 1
    if not _is_whole(line):
        if family == "negbin":
            r = _required(shape)
            q = float(special.betaincinv(k, r, p))
            return r * q / (1 - q)
        return float(special.gammaincinv(k, p))
    # A whole-number line: the probability is conditional on no push, so solve numerically.
    lower, upper = _LOCATION_BOUNDS

    def gap(log_mean: float) -> float:
        return _count_p_over(family, shape, math.exp(log_mean), line) - p

    return math.exp(optimize.brentq(gap, lower, upper, xtol=1e-12))


# Fitted distribution ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FittedDistribution:
    """A stat's distribution once its location is known.

    `location` is the gamma scale, the log-normal mu, or the mean of a count.
    """

    model: StatModel
    location: float
    zero_mass: float = 0.0

    @property
    def family(self) -> Family:
        return self.model.family

    @property
    def _shape(self) -> float:
        return _required(self.model.shape)

    # Probabilities --------------------------------------------------------------------------

    def p_over(self, line: float) -> float:
        """P(over `line`) as the books quote it (conditional on no push at a whole-number line)."""
        if self.model.continuous:
            if line <= 0:
                return 1 - self.zero_mass if line == 0 else 1.0
            tail = _tail_continuous(self.family, self._shape, self.location, line)
            return (1 - self.zero_mass) * tail
        return _count_p_over(self.family, self.model.shape, self.location, line)

    def sf(self, x: float) -> float:
        """P(X > x), with no push adjustment."""
        if self.model.continuous:
            return self.p_over(x)
        return _count_at_least(self.family, self.model.shape, self.location, math.floor(x) + 1)

    def cdf(self, x: float) -> float:
        return 1 - self.sf(x)

    def pit(self, x: float) -> float:
        """Where an outcome falls in the distribution, 0 to 1. A mass point (zero, or a count)
        gets the middle of its step, so outcomes from this distribution are uniform."""
        if self.model.continuous:
            if x <= 0:
                return self.zero_mass / 2
            return self.cdf(x)
        return (self.cdf(x) + self.cdf(x - 1)) / 2

    def slope(self, line: float) -> float:
        """How fast P(over) falls per unit of line: the density for yardage, and the probability
        of the one value between two half-point lines for a count."""
        if self.model.continuous:
            return (1 - self.zero_mass) * self._pdf(line)
        return self.sf(line - 0.5) - self.sf(line + 0.5)

    def _pdf(self, x: float) -> float:
        shape = self._shape
        if x <= 0:
            return 0.0
        if self.family == "gamma":
            log_pdf = (
                (shape - 1) * math.log(x)
                - x / self.location
                - math.lgamma(shape)
                - shape * math.log(self.location)
            )
            return math.exp(log_pdf)
        z = (math.log(x) - self.location) / shape
        return math.exp(-0.5 * z * z) / (x * shape * math.sqrt(2 * math.pi))

    # Moments and quantiles ------------------------------------------------------------------

    @property
    def mean(self) -> float:
        if self.family == "gamma":
            return (1 - self.zero_mass) * self._shape * self.location
        if self.family == "lognormal":
            return (1 - self.zero_mass) * math.exp(self.location + self._shape**2 / 2)
        return self.location

    @property
    def variance(self) -> float:
        if self.family == "gamma":
            second = self._shape * (self._shape + 1) * self.location**2
        elif self.family == "lognormal":
            second = math.exp(2 * self.location + 2 * self._shape**2)
        elif self.family == "negbin":
            return self.location + self.location**2 / self._shape
        else:
            return self.location
        return (1 - self.zero_mass) * second - self.mean**2

    def quantile(self, q: float) -> float:
        """The smallest value x with P(X <= x) >= q."""
        if not 0 < q < 1:
            raise ValueError("q must be strictly between 0 and 1")
        if self.model.continuous:
            if q <= self.zero_mass:
                return 0.0
            u = _clamp((q - self.zero_mass) / (1 - self.zero_mass), _PROBIT_EDGE)
            if self.family == "gamma":
                return float(self.location * special.gammaincinv(self._shape, u))
            return float(math.exp(self.location + self._shape * special.ndtri(u)))
        if self.family == "negbin":
            r = self._shape
            return float(stats.nbinom.ppf(q, r, r / (r + self.location)))
        return float(stats.poisson.ppf(q, self.location))

    def quantiles(self, q: NDArray[np.float64]) -> NDArray[np.float64]:
        """`quantile` for an array of probabilities in (0, 1): turns uniform draws into draws
        of the stat, which is how correlated draws are made."""
        q = np.clip(np.asarray(q, dtype=np.float64), _PROBIT_EDGE, 1 - _PROBIT_EDGE)
        if self.model.continuous:
            u = np.clip((q - self.zero_mass) / (1 - self.zero_mass), _PROBIT_EDGE, 1 - _PROBIT_EDGE)
            if self.family == "gamma":
                positive = self.location * special.gammaincinv(self._shape, u)
            else:
                positive = np.exp(self.location + self._shape * special.ndtri(u))
            return np.where(q <= self.zero_mass, 0.0, positive).astype(np.float64)
        if self.location <= 0:
            return np.zeros_like(q)
        if self.family == "negbin":
            r = self._shape
            return np.asarray(stats.nbinom.ppf(q, r, r / (r + self.location)), dtype=np.float64)
        return np.asarray(stats.poisson.ppf(q, self.location), dtype=np.float64)

    @property
    def median(self) -> float:
        return self.quantile(0.5)

    def summary(self) -> Summary:
        return Summary(self.mean, self.median, self.quantile(0.2), self.quantile(0.8))

    def sample(self, n: int, rng: np.random.Generator) -> NDArray[np.float64]:
        """`n` random draws, for simulating one stat against another."""
        if self.family == "gamma":
            positive = rng.gamma(self._shape, self.location, n)
        elif self.family == "lognormal":
            positive = rng.lognormal(self.location, self._shape, n)
        elif self.family == "negbin":
            r = self._shape
            return rng.negative_binomial(r, r / (r + self.location), n).astype(np.float64)
        else:
            return rng.poisson(self.location, n).astype(np.float64)
        return np.where(rng.random(n) < self.zero_mass, 0.0, positive)


# Fitting ---------------------------------------------------------------------------------------


def fit_anytime_td(p_td: float) -> FittedDistribution:
    """Anytime TD: the touchdown count is Poisson with lambda = -ln(1 - p), so P(at least one)
    is exactly the market's probability. Total TDs for rule 2 come from here."""
    return FittedDistribution(StatModel("poisson"), -math.log(1 - _clamp(p_td)))


def _zero_mass(model: StatModel, line: float, p: float) -> float:
    """The share of games at zero to assume at `line`, kept low enough for p to be reachable."""
    if model.zero_inflation is None:
        return 0.0
    return max(0.0, min(model.zero_inflation.at(line), 1 - p - _EDGE))


def fit_line(
    model: StatModel, line: float, p_over: float, zero_at: float | None = None
) -> FittedDistribution:
    """The distribution of the stat given P(over `line`) = `p_over` and the model's shape.

    The share of games at zero is read at `zero_at`, by default the line itself.
    """
    p = _clamp(p_over)
    if model.family == "bernoulli":
        return fit_anytime_td(p)
    if model.continuous:
        if line <= 0:
            raise ValueError("a yardage line must be positive")
        zero = _zero_mass(model, zero_at or line, p)
        tail = _clamp(p / (1 - zero))
        shape = _required(model.shape)
        return FittedDistribution(model, _solve_continuous(model.family, shape, line, tail), zero)
    if line < 0:
        raise ValueError("a count line can't be negative")
    if line == 0:
        raise ValueError("a count line of zero has no under: over zero is certain")
    return FittedDistribution(model, _solve_count(model.family, model.shape, line, p))


def _merge(points: Iterable[LinePoint]) -> list[LinePoint]:
    """One point per line (weighted mean probability), ignoring zero-weight points."""
    sums: dict[float, list[float]] = {}
    for point in points:
        if point.weight > 0:
            entry = sums.setdefault(point.line, [0.0, 0.0])
            entry[0] += point.weight * point.p_over
            entry[1] += point.weight
    return [LinePoint(line, p / w, w) for line, (p, w) in sorted(sums.items())]


def fit(
    model: StatModel, points: Sequence[LinePoint], prior_strength: float = 0.25
) -> FittedDistribution:
    """Fit location, and for most families shape, to every line of one player at once.

    With one line this is `fit_line`. With several, a weighted least-squares fit on the probit
    scale, with a pull toward the configured shape worth `prior_strength` extra observations, and
    the shape held within a factor of two of it. The zero share is read once, at the heaviest
    line (the main line, not an alternate).
    """
    merged = _merge(points)
    if not merged:
        raise ValueError("fit needs at least one point with positive weight")
    if model.family == "bernoulli":
        total = sum(p.weight for p in merged)
        return fit_anytime_td(sum(p.weight * p.p_over for p in merged) / total)
    anchor = max(merged, key=lambda p: p.weight)
    if len(merged) == 1:
        return fit_line(model, anchor.line, anchor.p_over)

    zero = _zero_mass(model, anchor.line, _clamp(anchor.p_over)) if model.continuous else 0.0
    targets = [_clamp(p.p_over / (1 - zero), _PROBIT_EDGE) for p in merged]
    if model.family == "lognormal":
        return _fit_lognormal(model, merged, targets, zero, prior_strength)
    start = fit_line(model, anchor.line, anchor.p_over).location
    return _fit_least_squares(model, merged, targets, zero, start, prior_strength)


def _fit_lognormal(
    model: StatModel, points: list[LinePoint], targets: list[float], zero: float, tau: float
) -> FittedDistribution:
    """Closed form: the probit of the tail is linear in ln(line), z = mu/sigma - ln(L)/sigma."""
    base_sigma = _required(model.shape)
    weights = [p.weight for p in points]
    total = sum(weights)
    x = [math.log(p.line) for p in points]
    z = [float(special.ndtri(t)) for t in targets]
    x_bar = sum(w * v for w, v in zip(weights, x, strict=True)) / total
    z_bar = sum(w * v for w, v in zip(weights, z, strict=True)) / total
    s_xx = sum(w * (v - x_bar) ** 2 for w, v in zip(weights, x, strict=True))
    s_xz = sum(w * (a - x_bar) * (b - z_bar) for w, a, b in zip(weights, x, z, strict=True))
    prior_slope = -1 / base_sigma
    slope = (s_xz + tau * s_xx * prior_slope) / (s_xx * (1 + tau))
    sigma = -1 / slope if slope < -1e-9 else base_sigma
    sigma = min(max(sigma, base_sigma / 2), base_sigma * 2)
    mu = sigma * (z_bar + x_bar / sigma)
    return FittedDistribution(StatModel("lognormal", sigma, model.zero_inflation), mu, zero)


def _fit_least_squares(
    model: StatModel,
    points: list[LinePoint],
    targets: list[float],
    zero: float,
    start: float,
    tau: float,
) -> FittedDistribution:
    family = model.family
    weights = np.array([p.weight for p in points])
    total = float(weights.sum())
    wanted = np.array([float(special.ndtri(t)) for t in targets])
    has_shape = model.shape is not None
    base_shape = model.shape or 1.0
    root_w = np.sqrt(weights)

    def p_over_at(location: float, shape: float | None, line: float) -> float:
        if family == "gamma":
            return _tail_continuous(family, _required(shape), location, line)
        return _count_p_over(family, shape, location, line)

    def residuals(v: NDArray[np.float64]) -> NDArray[np.float64]:
        location = math.exp(v[0])
        shape = math.exp(v[1]) if has_shape else None
        modelled = np.array(
            [
                float(special.ndtri(_clamp(p_over_at(location, shape, p.line), _PROBIT_EDGE)))
                for p in points
            ]
        )
        out = root_w * (modelled - wanted)
        if has_shape:
            out = np.append(out, math.sqrt(tau * total) * (v[1] - math.log(base_shape)))
        return np.asarray(out, dtype=np.float64)

    start_vector = [math.log(max(start, 1e-9))] + ([math.log(base_shape)] if has_shape else [])
    lower = [_LOCATION_BOUNDS[0]] + ([math.log(base_shape / 2)] if has_shape else [])
    upper = [_LOCATION_BOUNDS[1]] + ([math.log(base_shape * 2)] if has_shape else [])
    result = optimize.least_squares(residuals, start_vector, bounds=(lower, upper))
    location = math.exp(result.x[0])
    fitted_shape = math.exp(result.x[1]) if has_shape else None
    return FittedDistribution(StatModel(family, fitted_shape, model.zero_inflation), location, zero)


# For the consensus ------------------------------------------------------------------------------


def shift_probability(
    model: StatModel,
    p_over: float,
    from_line: float,
    to_line: float,
    zero_at: float | None = None,
) -> float:
    """P(over `to_line`) for a book whose price says P(over `from_line`) = `p_over`, under the
    stat's fitted distribution.

    The share of games at zero belongs to the player, not to a line, so one share is used for
    both ends: read at `zero_at`, or by default halfway between the lines in ln(line). With the
    default, shifting there and back returns the original.
    """
    if from_line == to_line:
        return _clamp(p_over)
    reference = zero_at or math.sqrt(from_line * to_line) if model.continuous else None
    return _clamp(fit_line(model, from_line, p_over, reference).p_over(to_line))


def line_slope(model: StatModel, p_over: float, line: float, zero_at: float | None = None) -> float:
    """The fitted distribution's slope at `line`; see `FittedDistribution.slope`."""
    return fit_line(model, line, p_over, zero_at).slope(line)


@dataclass(frozen=True)
class FittedLineModel:
    """The consensus' view of a stat: how a probability moves when the line moves."""

    model: StatModel

    def shift(self, p_over: float, from_line: float, to_line: float) -> float:
        """Every book is shifted to the consensus line, so the zero share is read there."""
        return shift_probability(self.model, p_over, from_line, to_line, zero_at=to_line)

    def slope(self, p_over: float, line: float) -> float:
        return line_slope(self.model, p_over, line)
