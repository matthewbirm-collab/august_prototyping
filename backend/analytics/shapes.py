"""Estimate the shape of each stat's distribution from weekly game logs.

`engine.distributions` needs a shape per stat (gamma shape, log-normal sigma, negative-binomial r)
and a zero-yard share, because one market line cannot pin the curve down. This estimates them
from nflverse player stats, one stat and position at a time.

The estimate is the spread of a player's own games within a season. That is an *upper bound* on
the spread the closing line implies: part of it is things the market already knows (matchup,
injuries, weather). Refit against real closing lines once the backfill has been bought; the
calibration report says how far to tighten.

Qualifying games stand in for "a game this player would have a prop for" (a QB who threw 15 or more
passes, a back with 5 or more carries...). Only player-seasons with `MIN_GAMES` such games count.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import polars as pl
from scipy import optimize, special, stats

MIN_GAMES = 8
OVERDISPERSION_INDEX = 1.15  # variance / mean above this, and significant, means negative binomial
OVERDISPERSION_P = 0.01
ZERO_BINS = ((0.0, 12.0), (12.0, 25.0), (25.0, 40.0), (40.0, 60.0), (60.0, math.inf))
MIN_BIN_GAMES = 100
PRIOR_GAMES = 8  # a player's recent median stands in for the line when measuring zero games
MIN_PRIOR_GAMES = 5


@dataclass(frozen=True)
class StatSpec:
    stat: str
    """The market stat, as in `markets.stat`."""
    position: str
    column: str
    """The column of nflverse weekly player stats."""
    usage: pl.Expr
    """Which of a player's games count as a game he would have a prop for."""
    kind: Literal["yardage", "count"]
    zero_usage: pl.Expr | None = None
    """For yardage: the wider set of games in which a zero is a real outcome."""


def _spec(
    stat: str,
    position: str,
    column: str,
    usage: pl.Expr,
    kind: Literal["yardage", "count"],
    zero_usage: pl.Expr | None = None,
) -> StatSpec:
    return StatSpec(stat, position, column, usage, kind, zero_usage)


SPECS: tuple[StatSpec, ...] = (
    _spec("pass_yds", "QB", "passing_yards", pl.col("attempts") >= 15, "yardage"),
    _spec(
        "rush_yds", "RB", "rushing_yards", pl.col("carries") >= 5, "yardage", pl.col("carries") >= 1
    ),
    _spec(
        "rush_yds", "QB", "rushing_yards", pl.col("carries") >= 3, "yardage", pl.col("carries") >= 1
    ),
    _spec(
        "rec_yds",
        "WR",
        "receiving_yards",
        pl.col("targets") >= 3,
        "yardage",
        pl.col("targets") >= 1,
    ),
    _spec(
        "rec_yds",
        "TE",
        "receiving_yards",
        pl.col("targets") >= 3,
        "yardage",
        pl.col("targets") >= 1,
    ),
    _spec(
        "rec_yds",
        "RB",
        "receiving_yards",
        pl.col("targets") >= 2,
        "yardage",
        pl.col("targets") >= 1,
    ),
    _spec("pass_tds", "QB", "passing_tds", pl.col("attempts") >= 15, "count"),
    _spec("pass_completions", "QB", "completions", pl.col("attempts") >= 15, "count"),
    _spec("interceptions", "QB", "passing_interceptions", pl.col("attempts") >= 15, "count"),
    _spec("rush_attempts", "RB", "carries", pl.col("carries") >= 5, "count"),
    _spec("rush_attempts", "QB", "carries", pl.col("attempts") >= 15, "count"),
    _spec("receptions", "WR", "receptions", pl.col("targets") >= 3, "count"),
    _spec("receptions", "TE", "receptions", pl.col("targets") >= 3, "count"),
    _spec("receptions", "RB", "receptions", pl.col("targets") >= 2, "count"),
)


@dataclass(frozen=True)
class ZeroTable:
    lines: tuple[float, ...]
    shares: tuple[float, ...]
    games: tuple[int, ...]


@dataclass(frozen=True)
class YardageEstimate:
    spec: StatSpec
    player_seasons: int
    games: int
    zero_share: float
    lognormal_sigma: float
    gamma_shape: float
    loglik_lognormal: float
    loglik_gamma: float
    zero_table: ZeroTable | None

    @property
    def family(self) -> Literal["gamma", "lognormal"]:
        return "gamma" if self.loglik_gamma >= self.loglik_lognormal else "lognormal"

    @property
    def shape(self) -> float:
        return self.gamma_shape if self.family == "gamma" else self.lognormal_sigma


@dataclass(frozen=True)
class CountEstimate:
    spec: StatSpec
    player_seasons: int
    games: int
    mean: float
    dispersion_index: float
    p_overdispersed: float
    negbin_r: float | None

    @property
    def family(self) -> Literal["negbin", "poisson"]:
        overdispersed = (
            self.dispersion_index > OVERDISPERSION_INDEX
            and self.p_overdispersed < OVERDISPERSION_P
            and self.negbin_r is not None
        )
        return "negbin" if overdispersed else "poisson"


Estimate = YardageEstimate | CountEstimate


def qualified_games(df: pl.DataFrame, spec: StatSpec) -> pl.DataFrame:
    """The (player_id, season, x) rows of player-seasons with at least `MIN_GAMES` qualifying
    games, where x is the stat."""
    games = df.filter((pl.col("position") == spec.position) & spec.usage).select(
        "player_id", "season", pl.col(spec.column).cast(pl.Float64).alias("x")
    )
    enough = games.group_by("player_id", "season").len().filter(pl.col("len") >= MIN_GAMES)
    return games.join(enough.select("player_id", "season"), on=["player_id", "season"])


def estimate_yardage(df: pl.DataFrame, spec: StatSpec) -> YardageEstimate:
    """Log-normal sigma and gamma shape from the spread of a player's own positive games, and
    which family describes the games better.

    Zero-yard games are left out of the shape (the zero share is modelled separately), and each
    player-season gets its own location, so only the spread is pooled. Reported shapes are
    corrected for the location being estimated (degrees of freedom); the likelihoods used to
    compare the families are plain maximum likelihood.
    """
    games = qualified_games(df, spec)
    positive = (
        games.filter(pl.col("x") > 0)
        .with_columns(pl.col("x").log().alias("lx"))
        .with_columns(
            pl.col("lx").mean().over("player_id", "season").alias("m_log"),
            pl.col("x").mean().over("player_id", "season").alias("m"),
            pl.len().over("player_id", "season").alias("n"),
        )
        .filter(pl.col("n") >= MIN_GAMES)
    )
    count = positive.height
    if count == 0:
        raise ValueError(f"no qualifying games for {spec.stat} {spec.position}")
    seasons = positive.select("player_id", "season").unique().height
    dof = count - seasons

    sum_squares = float(((positive["lx"] - positive["m_log"]) ** 2).sum())
    sigma_report = math.sqrt(sum_squares / dof)
    sigma_mle = math.sqrt(sum_squares / count)

    ratio = positive["x"] / positive["m"]
    gap = float((ratio - 1 - ratio.log()).sum())

    def shape_equation(k: float, total: int) -> float:
        return math.log(k) - float(special.digamma(k)) - gap / total

    shape_mle = float(optimize.brentq(shape_equation, 1e-3, 1e5, args=(count,)))
    shape_report = float(optimize.brentq(shape_equation, 1e-3, 1e5, args=(dof,)))

    sum_log = float(positive["lx"].sum())
    ll_lognormal = -sum_log - count * math.log(sigma_mle * math.sqrt(2 * math.pi)) - count / 2
    k = shape_mle
    ll_gamma = float(
        (
            (k - 1) * positive["lx"]
            - k * positive["x"] / positive["m"]
            - math.lgamma(k)
            - k * (positive["m"] / k).log()
        ).sum()
    )
    zero_share = float((games["x"] <= 0).sum()) / games.height
    return YardageEstimate(
        spec=spec,
        player_seasons=seasons,
        games=count,
        zero_share=zero_share,
        lognormal_sigma=sigma_report,
        gamma_shape=shape_report,
        loglik_lognormal=ll_lognormal,
        loglik_gamma=ll_gamma,
        zero_table=zero_table(df, spec),
    )


def estimate_count(df: pl.DataFrame, spec: StatSpec) -> CountEstimate:
    """The index of dispersion (variance / mean, 1 for a Poisson) within player-seasons, tested
    against 1, and the negative-binomial r that would explain any excess."""
    games = qualified_games(df, spec)
    groups = (
        games.group_by("player_id", "season")
        .agg(
            pl.col("x").mean().alias("m"),
            ((pl.col("x") - pl.col("x").mean()) ** 2).sum().alias("ss"),
            pl.len().alias("n"),
        )
        .filter(pl.col("m") > 0)
    )
    if groups.is_empty():
        raise ValueError(f"no qualifying games for {spec.stat} {spec.position}")
    dof = int((groups["n"] - 1).sum())
    chi = float((groups["ss"] / groups["m"]).sum())
    variance = groups["ss"] / (groups["n"] - 1)
    excess = float((variance - groups["m"]).sum())
    r = float((groups["m"] ** 2).sum()) / excess if excess > 0 else None
    return CountEstimate(
        spec=spec,
        player_seasons=groups.height,
        games=int(groups["n"].sum()),
        mean=float(groups["m"].mean()),  # type: ignore[arg-type]
        dispersion_index=chi / dof,
        p_overdispersed=float(stats.chi2.sf(chi, dof)),
        negbin_r=r,
    )


def zero_table(df: pl.DataFrame, spec: StatSpec) -> ZeroTable | None:
    """The share of zero-stat games by the size of the player's recent median, which stands in
    for the posted line. Shares fall as the line rises, so they are smoothed to do that.

    A player who was active but had no target or carry has no row in the game logs, so these
    shares understate the true share of zeros.
    """
    if spec.zero_usage is None:
        return None
    rows = (
        df.filter((pl.col("position") == spec.position) & spec.zero_usage)
        .sort("player_id", "season", "week")
        .select("player_id", pl.col(spec.column).cast(pl.Float64).alias("x"))
        .with_columns(
            pl.col("x")
            .shift(1)
            .rolling_median(PRIOR_GAMES, min_samples=MIN_PRIOR_GAMES)
            .over("player_id")
            .alias("prior")
        )
        .drop_nulls("prior")
    )
    lines: list[float] = []
    shares: list[float] = []
    games: list[int] = []
    for low, high in ZERO_BINS:
        inside = rows.filter((pl.col("prior") >= low) & (pl.col("prior") < high))
        if inside.height < MIN_BIN_GAMES:
            continue
        lines.append(float(inside["prior"].median()))  # type: ignore[arg-type]
        shares.append(float((inside["x"] <= 0).sum()) / inside.height)
        games.append(inside.height)
    if len(lines) < 2:
        return None
    smoothed = non_increasing(shares, games)
    return ZeroTable(tuple(lines), tuple(smoothed), tuple(games))


def non_increasing(values: Sequence[float], weights: Sequence[int]) -> list[float]:
    """The closest non-increasing sequence (weighted least squares): pool adjacent violators."""
    blocks: list[list[float]] = []  # [mean, weight, count]
    for value, weight in zip(values, weights, strict=True):
        blocks.append([value, float(weight), 1.0])
        while len(blocks) > 1 and blocks[-2][0] < blocks[-1][0]:
            last, previous = blocks.pop(), blocks.pop()
            total = last[1] + previous[1]
            blocks.append(
                [
                    (last[0] * last[1] + previous[0] * previous[1]) / total,
                    total,
                    last[2] + previous[2],
                ]
            )
    out: list[float] = []
    for mean, _, count in blocks:
        out.extend([mean] * int(count))
    return out


def estimate_all(df: pl.DataFrame, specs: Sequence[StatSpec] = SPECS) -> list[Estimate]:
    return [
        estimate_yardage(df, spec) if spec.kind == "yardage" else estimate_count(df, spec)
        for spec in specs
    ]


# The config file -------------------------------------------------------------------------------

# Positions with no estimate of their own borrow another's shape.
DEFAULT_POSITION = {"rush_yds": "RB", "rec_yds": "WR", "rush_attempts": "RB", "receptions": "WR"}


def entry(estimate: Estimate, *, round_to: int = 3) -> dict[str, Any]:
    """One stat/position's entry for `config/distribution_params.json`."""
    if isinstance(estimate, YardageEstimate):
        result: dict[str, Any] = {
            "family": estimate.family,
            "shape": round(estimate.shape, round_to),
        }
        table = estimate.zero_table
        if table is not None:
            result["zero_inflation"] = {
                "lines": [round(v, 1) for v in table.lines],
                "shares": [round(v, 4) for v in table.shares],
            }
        return result
    if estimate.family == "negbin":
        return {"family": "negbin", "shape": round(estimate.negbin_r or 0, 2)}
    return {"family": "poisson"}


def build_params(estimates: Sequence[Estimate], *, source: str, generated: str) -> dict[str, Any]:
    stats_block: dict[str, dict[str, Any]] = {}
    for estimate in estimates:
        stats_block.setdefault(estimate.spec.stat, {})[estimate.spec.position] = entry(estimate)
    for stat, by_position in stats_block.items():
        wanted = DEFAULT_POSITION.get(stat)
        by_position["default"] = (
            by_position[wanted] if wanted in by_position else next(iter(by_position.values()))
        )
    stats_block["anytime_td"] = {"default": {"family": "bernoulli"}}
    return {
        "version": 1,
        "source": source,
        "generated": generated,
        "note": (
            "Shapes are the spread of a player's own games within a season: an upper bound on "
            "the spread a closing line implies. Refit against real lines after the backfill."
        ),
        "stats": stats_block,
    }


# The evidence document -------------------------------------------------------------------------


def render_report(
    estimates: Sequence[Estimate], *, source: str, generated: str, seasons: str
) -> str:
    """Markdown for `docs/DISTRIBUTION_SHAPES.md`: what was estimated, from what, how to read it."""
    yardage = [e for e in estimates if isinstance(e, YardageEstimate)]
    counts = [e for e in estimates if isinstance(e, CountEstimate)]
    lines = [
        "# Distribution shapes",
        "",
        f"Generated {generated} by `scripts/estimate_distribution_shapes.py` from {source}, "
        f"regular seasons {seasons}. The shipped values are in "
        "`backend/config/distribution_params.json`.",
        "",
        "`engine/distributions.py` turns the market's probability at one line into a full curve, "
        "which needs a **shape** the line cannot supply: how spread out a stat is for a given "
        "level. These are those shapes.",
        "",
        "**Read these as upper bounds.** Each is the spread of a player's *own games within a "
        f"season* (player-seasons with at least {MIN_GAMES} qualifying games). Part of that "
        "spread is matchup, injury and weather, which the closing line already prices in, so "
        "the spread conditional on the line is smaller. After the backfill, `calibration-report` "
        "shows whether the fitted ranges cover about 60% of outcomes between p20 and p80, and "
        "how far to tighten.",
        "",
        "## Yardage: gamma or log-normal",
        "",
        "Zero-yard games are left out of the shape (they are modelled as a separate share), and "
        "each player-season gets its own average, so only the spread is pooled. The family with "
        "the higher log-likelihood is chosen.",
        "",
        "| stat | pos | player-seasons | games | zero games | log-normal sigma | gamma shape k | "
        "log-lik gamma minus log-normal | family |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for e in yardage:
        lines.append(
            f"| {e.spec.stat} | {e.spec.position} | {e.player_seasons} | {e.games:,} | "
            f"{e.zero_share:.1%} | {e.lognormal_sigma:.3f} | {e.gamma_shape:.2f} | "
            f"{e.loglik_gamma - e.loglik_lognormal:+.0f} | **{e.family}** |"
        )
    lines += [
        "",
        "A smaller gamma shape means a wider spread (coefficient of variation = 1 / sqrt(k)).",
        "",
        "## Counts: Poisson or negative binomial",
        "",
        "The index of dispersion is variance / mean within player-seasons: 1 for a Poisson, above "
        f"1 for more spread than Poisson. A stat is negative binomial when the index is above "
        f"{OVERDISPERSION_INDEX} and a chi-square test rejects 1 at p < {OVERDISPERSION_P}.",
        "",
        "| stat | pos | player-seasons | mean | dispersion index | p (over-dispersed) | "
        "negative-binomial r | family |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for c in counts:
        r = f"{c.negbin_r:.1f}" if c.negbin_r is not None else "—"
        lines.append(
            f"| {c.spec.stat} | {c.spec.position} | {c.player_seasons} | {c.mean:.2f} | "
            f"{c.dispersion_index:.2f} | {c.p_overdispersed:.2g} | {r} | **{c.family}** |"
        )
    lines += [
        "",
        "Receptions and pass touchdowns come out *under*-dispersed (below 1), so Poisson is a "
        "little wider than the data. Anytime TD has no shape: the touchdown count is Poisson with "
        "lambda = -ln(1 - p).",
        "",
        "## Zero-yard games by line",
        "",
        "The share of games ending at zero by the size of the player's recent median (a stand-in "
        "for the posted line), smoothed so it never rises with the line. Low-volume players have "
        "low lines and often finish with nothing. These understate the true share: a player who "
        "was active but had no target or carry has no row in the game logs.",
    ]
    for e in yardage:
        table = e.zero_table
        if table is None:
            continue
        lines += [
            "",
            f"**{e.spec.stat} ({e.spec.position})**",
            "",
            "| typical line | zero share | games |",
            "| ---: | ---: | ---: |",
        ]
        lines += [
            f"| {line:.1f} | {share:.1%} | {games:,} |"
            for line, share, games in zip(table.lines, table.shares, table.games, strict=True)
        ]
    lines += [
        "",
        "## Refreshing",
        "",
        "```bash",
        "cd backend && uv run python scripts/estimate_distribution_shapes.py",
        "```",
        "",
    ]
    return "\n".join(lines)
