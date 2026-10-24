"""Odds conversion and de-vigging. Pure: no I/O, and nothing from `db/`, `app/` or `ingest/`.

A sportsbook's over/under prices, turned into probabilities, add up to more than 1; the excess
is the book's margin (the vig). *De-vigging* removes it to get fair probabilities that sum to 1.
How to split the margin between the two sides is a modelling choice, and the methods differ
mainly on lopsided markets, because books shade longshots harder than favorites (the
favorite-longshot bias):

====================  ==========================  ===============================================
method                fair P(over)                effect on the longshot
====================  ==========================  ===============================================
``multiplicative``    a / S                       the same *proportion* comes off both sides, so
                                                  the longshot stays a little too likely
``additive``          a - (S - 1) / 2             the same *amount* comes off both sides
``shin``              Shin (1992) insider model   identical to ``additive`` for two outcomes
``power``             a**k, with a**k + b**k = 1  the longshot is cut hardest
====================  ==========================  ===============================================

with ``a`` and ``b`` the raw probabilities and ``S = a + b``. At -110/-110 every method gives
0.5. On -200/+160 the fair over probability is 0.6341 (multiplicative), 0.6410 (additive and
Shin) and 0.6446 (power). On an anytime-TD line of Yes +1200 / No -3000 the probability of a
touchdown is 7.4%, 5.5% or 4.0%, so the choice matters most for heavy favorites and longshots.
"""

import math
from typing import Final, Literal, NamedTuple

Method = Literal["multiplicative", "additive", "power", "shin"]
METHODS: Final[tuple[Method, ...]] = ("multiplicative", "additive", "power", "shin")

# A pair of probabilities summing to less than 1 by more than this is not a real two-way price.
_SUM_TOLERANCE: Final = 1e-9
# At or below this much margin a pair is already fair, and every method returns the same thing.
_NO_MARGIN: Final = 1e-12
_MAX_SOLVER_STEPS: Final = 200


class InvalidOddsError(ValueError):
    """The prices or probabilities can't be a real two-way market (bad data)."""


class FairProbabilities(NamedTuple):
    over: float
    under: float


# Conversions -----------------------------------------------------------------------------------


def _check_american(odds: float) -> None:
    if not math.isfinite(odds) or abs(odds) < 100:
        raise InvalidOddsError(
            f"American odds must be finite with a magnitude of at least 100, got {odds!r}"
        )


def american_to_decimal(odds: float) -> float:
    """Decimal odds (the payout per unit staked, stake included): +150 -> 2.5, -200 -> 1.5."""
    _check_american(odds)
    return 1 + odds / 100 if odds > 0 else 1 + 100 / -odds


def decimal_to_american(decimal: float) -> int:
    """American odds, rounded to a whole number: 2.5 -> +150, 1.5 -> -200.

    Even money (2.0) is returned as +100, so -100 becomes +100 on a round trip; they mean the
    same thing.
    """
    if not math.isfinite(decimal) or decimal <= 1:
        raise InvalidOddsError(f"decimal odds must be finite and above 1, got {decimal!r}")
    if decimal >= 2:
        return math.floor((decimal - 1) * 100 + 0.5)
    return -math.floor(100 / (decimal - 1) + 0.5)


def american_to_implied_prob(odds: float) -> float:
    """The probability a price implies, vig included: -110 -> 0.5238, +150 -> 0.4."""
    _check_american(odds)
    return 100 / (odds + 100) if odds > 0 else -odds / (100 - odds)


# Margin ----------------------------------------------------------------------------------------


def _check_probability(p: float, name: str) -> None:
    if not math.isfinite(p) or not 0 < p < 1:
        raise InvalidOddsError(f"{name} must be strictly between 0 and 1, got {p!r}")


def overround(p_over_raw: float, p_under_raw: float) -> float:
    """How far the raw probabilities exceed 1: 0.0476 at -110/-110. Negative if they fall short."""
    _check_probability(p_over_raw, "p_over_raw")
    _check_probability(p_under_raw, "p_under_raw")
    return p_over_raw + p_under_raw - 1


def hold(p_over_raw: float, p_under_raw: float) -> float:
    """The book's margin as a share of the money bet: 1 - 1/S, which is 0.0455 at -110/-110.

    This is the figure usually quoted as "the vig". `overround` is the same margin measured against
    the fair total instead (0.0476 at -110/-110). A negative result means the pair is not a real
    two-way price (the book would lose on both sides).
    """
    total = overround(p_over_raw, p_under_raw) + 1
    return 1 - 1 / total


# De-vigging ------------------------------------------------------------------------------------


def power_exponent(p_over_raw: float, p_under_raw: float) -> float:
    """The k >= 1 for which p_over_raw**k + p_under_raw**k = 1 (the power method's exponent)."""
    a, b = p_over_raw, p_under_raw
    if a + b - 1 <= _NO_MARGIN:
        return 1.0

    def excess(k: float) -> float:
        return math.exp(k * math.log(a)) + math.exp(k * math.log(b)) - 1

    # The excess falls from a + b - 1 > 0 at k = 1 towards -1, so a single root lies above 1.
    low, high = 1.0, 2.0
    while excess(high) > 0:
        low, high = high, high * 2
    for _ in range(_MAX_SOLVER_STEPS):
        middle = (low + high) / 2
        if middle in (low, high):
            break
        if excess(middle) > 0:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def shin_insider_share(p_over_raw: float, p_under_raw: float) -> float:
    """Shin's z, the share of money assumed to come from insiders, for two outcomes.

    Closed form for n = 2: z = (S - 1)(D^2 - S) / (S(D^2 - 1)), with D = a - b. It is 0 when there
    is no margin and approaches 1 only for absurd margins.
    """
    a, b = p_over_raw, p_under_raw
    total = a + b
    if total - 1 <= _NO_MARGIN:
        return 0.0
    d_squared = (a - b) ** 2
    return float((total - 1) * (d_squared - total) / (total * (d_squared - 1)))


def _shin_probability(p_raw: float, total: float, z: float) -> float:
    """Shin's fair probability for one outcome, written as 2q / (sqrt(z^2 + 4(1-z)q) + z) with
    q = p_raw^2 / total, which avoids the cancellation in the textbook form as z nears 1."""
    q = p_raw * p_raw / total
    return 2 * q / (math.sqrt(z * z + 4 * (1 - z) * q) + z)


def devig_two_way(
    p_over_raw: float, p_under_raw: float, method: Method = "multiplicative"
) -> FairProbabilities:
    """Fair probabilities for the over and the under, summing to 1.

    The inputs are the raw probabilities the two prices imply (`american_to_implied_prob`), each
    strictly between 0 and 1 and together at least 1. A pair summing to less than 1 raises
    `InvalidOddsError`: a real book never pays out more than it takes in, so it means bad data
    (for example two quotes from different moments). An unknown `method` raises `ValueError`.

    Every method returns the over probability in (0, 1), preserves the order of the two sides,
    swaps its answers when the sides are swapped, and leaves an already-fair pair unchanged.
    See the module docstring for how the methods differ.
    """
    if method not in METHODS:
        raise ValueError(f"unknown de-vig method {method!r}; choose from {', '.join(METHODS)}")
    a, b = p_over_raw, p_under_raw
    margin = overround(a, b)
    if margin < -_SUM_TOLERANCE:
        raise InvalidOddsError(
            f"over and under probabilities sum to {a + b:.6f}, below 1: not a two-way price"
        )
    total = a + b
    if margin <= _NO_MARGIN or method == "multiplicative":
        over = a / total  # with no margin to remove, every method gives this
    elif method == "additive":
        over = 0.5 + (a - b) / 2  # = a - (total - 1) / 2, and always inside (0, 1)
    elif method == "power":
        over = math.exp(power_exponent(a, b) * math.log(a))
    else:
        over = _shin_probability(a, total, shin_insider_share(a, b))
    return FairProbabilities(over, 1 - over)
