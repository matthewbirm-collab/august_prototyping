import json
import math
from collections.abc import Callable

import numpy as np
import polars as pl
import pytest

from analytics import shapes
from analytics.shapes import (
    MIN_GAMES,
    SPECS,
    CountEstimate,
    StatSpec,
    YardageEstimate,
    build_params,
    estimate_all,
    estimate_count,
    estimate_yardage,
    non_increasing,
    render_report,
    zero_table,
)

RUSH_RB = next(s for s in SPECS if s.stat == "rush_yds" and s.position == "RB")
RECEPTIONS_WR = next(s for s in SPECS if s.stat == "receptions" and s.position == "WR")
COMPLETIONS = next(s for s in SPECS if s.stat == "pass_completions")


Draw = Callable[[np.random.Generator, float, int], np.ndarray]


def games_frame(
    draw: Draw,
    players: int = 300,
    games: int = 16,
    position: str = "RB",
    seed: int = 1,
    column: str = "rushing_yards",
) -> pl.DataFrame:
    """Player-games where `draw(rng, level, n)` gives a player's stats (in `column`) and
    everyone has plenty of usage."""
    rng = np.random.default_rng(seed)
    rows: list[dict[str, object]] = []
    for player in range(players):
        level = float(rng.uniform(30, 90))
        values = draw(rng, level, games)
        for week, value in enumerate(values, start=1):
            rows.append(
                {
                    "player_id": f"p{player}",
                    "season": 2024,
                    "week": week,
                    "position": position,
                    "carries": 12,
                    "targets": 6,
                    "attempts": 30,
                    column: float(value),
                }
            )
    return pl.DataFrame(rows)


def gamma_draw(shape: float) -> Draw:
    return lambda rng, level, n: rng.gamma(shape, level / shape, n)


def lognormal_draw(sigma: float) -> Draw:
    return lambda rng, level, n: rng.lognormal(math.log(level), sigma, n)


# Yardage ---------------------------------------------------------------------------------------


def test_gamma_data_gives_the_gamma_shape_and_the_gamma_family() -> None:
    df = games_frame(gamma_draw(3.0))
    estimate = estimate_yardage(df, RUSH_RB)

    assert estimate.gamma_shape == pytest.approx(3.0, rel=0.06)
    assert estimate.family == "gamma"
    assert estimate.loglik_gamma > estimate.loglik_lognormal
    assert estimate.shape == estimate.gamma_shape
    assert (estimate.player_seasons, estimate.games) == (300, 4800)


def test_log_normal_data_gives_sigma_and_the_log_normal_family() -> None:
    df = games_frame(lognormal_draw(0.6))
    estimate = estimate_yardage(df, RUSH_RB)

    assert estimate.lognormal_sigma == pytest.approx(0.6, rel=0.05)
    assert estimate.family == "lognormal"
    assert estimate.shape == estimate.lognormal_sigma


def test_short_seasons_are_corrected_for_estimating_each_players_average() -> None:
    """With only eight games per player, using each player's own average would understate the
    spread by about one eighth if it were not corrected for."""
    df = games_frame(gamma_draw(3.0), players=900, games=MIN_GAMES)
    assert estimate_yardage(df, RUSH_RB).gamma_shape == pytest.approx(3.0, rel=0.06)
    df = games_frame(
        lognormal_draw(0.6),
        players=900,
        games=MIN_GAMES,
    )
    assert estimate_yardage(df, RUSH_RB).lognormal_sigma == pytest.approx(0.6, rel=0.04)


def test_zero_games_are_counted_but_do_not_change_the_shape() -> None:
    rng = np.random.default_rng(5)

    def with_zeros(r: np.random.Generator, level: float, n: int) -> np.ndarray:
        values = r.gamma(3.0, level / 3.0, n)
        return np.where(rng.random(n) < 0.1, 0.0, values)

    df = games_frame(with_zeros)
    estimate = estimate_yardage(df, RUSH_RB)
    assert estimate.zero_share == pytest.approx(0.1, abs=0.015)
    assert estimate.gamma_shape == pytest.approx(3.0, rel=0.07)


def test_only_qualifying_games_of_qualifying_player_seasons_count() -> None:
    df = games_frame(gamma_draw(3.0), players=60)
    few = pl.DataFrame(
        [
            {
                "player_id": "short",
                "season": 2024,
                "week": w,
                "position": "RB",
                "carries": 12,
                "targets": 6,
                "attempts": 30,
                "rushing_yards": 50.0,
            }
            for w in range(1, MIN_GAMES)  # one game short
        ]
    )
    light = (
        df.filter(pl.col("player_id") == "p0")
        .with_columns(pl.lit(2, dtype=pl.Int64).alias("carries"))
        .with_columns(pl.lit("light").alias("player_id"))
    )
    wrong_position = df.head(40).with_columns(pl.lit("WR").alias("position"))
    combined = pl.concat([df, few, light, wrong_position])

    estimate = estimate_yardage(combined, RUSH_RB)
    assert estimate.player_seasons == 60  # not the short season, the low-usage one, or the WRs


def test_no_qualifying_games_is_an_error() -> None:
    df = games_frame(gamma_draw(3.0), players=3, games=3)
    with pytest.raises(ValueError, match="no qualifying games"):
        estimate_yardage(df, RUSH_RB)
    receptions = games_frame(
        gamma_draw(3.0), players=3, games=3, position="WR", column="receptions"
    )
    with pytest.raises(ValueError, match="no qualifying games"):
        estimate_count(receptions, RECEPTIONS_WR)


# Counts ----------------------------------------------------------------------------------------


def count_frame(draw: Draw, column: str, position: str = "WR") -> pl.DataFrame:
    return games_frame(draw, players=300, position=position, column=column)


def test_poisson_counts_are_not_over_dispersed() -> None:
    df = count_frame(lambda rng, level, n: rng.poisson(level / 8, n), "receptions")
    estimate = estimate_count(df, RECEPTIONS_WR)
    assert estimate.dispersion_index == pytest.approx(1.0, abs=0.05)
    assert estimate.p_overdispersed > 0.01
    assert estimate.family == "poisson"


def test_over_dispersed_counts_give_a_negative_binomial_and_its_r() -> None:
    mean, r = 13.0, 10.0

    def negbin(rng: np.random.Generator, level: float, n: int) -> np.ndarray:
        return rng.negative_binomial(r, r / (r + mean), n)

    estimate = estimate_count(count_frame(negbin, "completions", "QB"), COMPLETIONS)

    assert estimate.dispersion_index == pytest.approx(
        1 + mean / r, rel=0.08
    )  # variance = m + m^2/r
    assert estimate.p_overdispersed < 1e-6
    assert estimate.family == "negbin"
    assert estimate.negbin_r == pytest.approx(r, rel=0.2)


def test_under_dispersed_counts_stay_poisson() -> None:
    df = count_frame(
        lambda rng, level, n: rng.binomial(10, 0.4, n), "receptions"
    )  # var 2.4, mean 4
    estimate = estimate_count(df, RECEPTIONS_WR)
    assert estimate.dispersion_index == pytest.approx(0.6, abs=0.05)
    assert estimate.p_overdispersed > 0.99
    assert estimate.negbin_r is None
    assert estimate.family == "poisson"


def test_a_small_excess_is_not_enough_for_a_negative_binomial() -> None:
    estimate = CountEstimate(RECEPTIONS_WR, 100, 1600, 4.0, 1.10, 1e-9, 40.0)
    assert estimate.family == "poisson"  # significant but below the 1.15 threshold
    assert CountEstimate(RECEPTIONS_WR, 100, 1600, 4.0, 1.30, 0.2, 40.0).family == "poisson"
    assert CountEstimate(RECEPTIONS_WR, 100, 1600, 4.0, 1.30, 1e-9, 40.0).family == "negbin"


# Zero-yard share by line -----------------------------------------------------------------------


def zero_games(level_to_zero_share: Callable[[float], float]) -> pl.DataFrame:
    """Players who each have a steady level; the chance of a zero depends on it."""
    rng = np.random.default_rng(11)

    def draw(r: np.random.Generator, level: float, n: int) -> np.ndarray:
        values = r.gamma(4.0, level / 4.0, n)
        return np.where(rng.random(n) < level_to_zero_share(level), 0.0, values)

    return games_frame(draw, players=700, games=17)


def test_the_zero_share_falls_as_the_line_rises() -> None:
    df = zero_games(lambda level: 0.30 if level < 40 else 0.05)
    table = zero_table(df, RUSH_RB)

    assert table is not None
    assert list(table.lines) == sorted(table.lines)
    assert list(table.shares) == sorted(table.shares, reverse=True)
    assert table.shares[0] > 3 * table.shares[-1]
    assert all(g >= shapes.MIN_BIN_GAMES for g in table.games)


def test_a_spec_without_zero_usage_has_no_table() -> None:
    pass_yds = next(s for s in SPECS if s.stat == "pass_yds")
    assert pass_yds.zero_usage is None
    assert zero_table(games_frame(gamma_draw(3.0)), pass_yds) is None


def test_too_little_data_gives_no_table() -> None:
    df = games_frame(gamma_draw(3.0), players=3, games=10)
    assert zero_table(df, RUSH_RB) is None


@pytest.mark.parametrize(
    ("values", "weights", "expected"),
    [
        ([0.2, 0.1, 0.05], [1, 1, 1], [0.2, 0.1, 0.05]),
        ([0.2, 0.1, 0.15, 0.05], [1, 1, 1, 1], [0.2, 0.125, 0.125, 0.05]),
        ([0.1, 0.3], [1, 1], [0.2, 0.2]),
        ([0.1, 0.3], [3, 1], [0.15, 0.15]),
        ([0.05], [10], [0.05]),
    ],
)
def test_pool_adjacent_violators(
    values: list[float], weights: list[int], expected: list[float]
) -> None:
    assert non_increasing(values, weights) == pytest.approx(expected)


# The config and the report ---------------------------------------------------------------------


def synthetic_everything() -> pl.DataFrame:
    """Enough of every column for every spec."""
    rng = np.random.default_rng(3)
    rows = []
    for position in ("QB", "RB", "WR", "TE"):
        for player in range(120):
            level = float(rng.uniform(0.5, 1.5))
            for week in range(1, 17):
                rows.append(
                    {
                        "player_id": f"{position}{player}",
                        "season": 2024,
                        "week": week,
                        "position": position,
                        "attempts": 30,
                        "carries": 10,
                        "targets": 6,
                        "passing_yards": float(rng.gamma(10, 25 * level)),
                        "rushing_yards": float(rng.gamma(3, 22 * level)),
                        "receiving_yards": float(rng.gamma(3, 15 * level)),
                        "passing_tds": float(rng.poisson(1.5 * level)),
                        "completions": float(rng.negative_binomial(40, 40 / (40 + 21 * level))),
                        "passing_interceptions": float(rng.poisson(0.7)),
                        "receptions": float(rng.poisson(4 * level)),
                    }
                )
    return pl.DataFrame(rows)


def test_the_config_covers_every_stat_and_position_and_serialises() -> None:
    estimates = estimate_all(synthetic_everything())
    params = build_params(estimates, source="synthetic", generated="2026-10-24")
    json.dumps(params)  # plain data

    assert params["version"] == 1
    assert set(params["stats"]) == {
        "pass_yds", "rush_yds", "rec_yds", "pass_tds", "pass_completions",
        "interceptions", "rush_attempts", "receptions", "anytime_td",
    }  # fmt: skip
    for stat, by_position in params["stats"].items():
        assert "default" in by_position, stat
    assert params["stats"]["anytime_td"] == {"default": {"family": "bernoulli"}}
    assert params["stats"]["rush_yds"]["default"] == params["stats"]["rush_yds"]["RB"]
    assert params["stats"]["pass_completions"]["QB"]["family"] == "negbin"
    assert params["stats"]["receptions"]["WR"] == {"family": "poisson"}
    assert params["stats"]["pass_yds"]["QB"]["family"] == "gamma"


def test_yardage_entries_carry_the_zero_table_when_there_is_one() -> None:
    estimates = estimate_all(synthetic_everything())
    for estimate in estimates:
        entry = shapes.entry(estimate)
        if isinstance(estimate, YardageEstimate) and estimate.zero_table is not None:
            assert set(entry["zero_inflation"]) == {"lines", "shares"}
        else:
            assert "zero_inflation" not in entry


def test_the_report_has_every_section_and_a_row_per_stat() -> None:
    estimates = estimate_all(synthetic_everything())
    text = render_report(estimates, source="synthetic logs", generated="2026-10-24", seasons="2024")

    for heading in (
        "# Distribution shapes",
        "## Yardage",
        "## Counts",
        "## Zero-yard games",
        "## Refreshing",
    ):
        assert heading in text
    assert "upper bounds" in text
    for spec in SPECS:
        assert f"| {spec.stat} | {spec.position} |" in text
    assert text.count("\n| rec_yds | WR |") == 1 or "| rec_yds | WR |" in text


def test_every_spec_names_columns_the_game_logs_have() -> None:
    columns = set(synthetic_everything().columns)
    for spec in SPECS:
        assert spec.column in columns
    assert all(isinstance(spec, StatSpec) for spec in SPECS)
