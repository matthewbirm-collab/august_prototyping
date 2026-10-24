"""Tune the gap-fill model's knobs on one season, holding the others out.

    cd backend && uv run python scripts/tune_gapfill.py --season 2023

Loads each finished game's usage as of 1 minute before kickoff once (cached in
`.cache/tuning/`), then scores a grid of `FillConfig` settings by PPR MAE per position from
the stat means alone, which is fast because it needs no simulation. Prints the grid and the best
setting. Use it on the tuning season only; report on the held-out seasons with `backtest`.
"""

import argparse
import itertools
import pickle
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

import numpy as np
from scipy import special
from sqlalchemy import select
from sqlalchemy.orm import Session

from analytics.backtest_cli import Backtest
from app.distribution_params import DEFAULT_PATH, load_distribution_params
from db.models import ActualStats
from db.session import get_engine
from engine.distributions import StatModel
from engine.gapfill import (
    FILLABLE,
    PRIOR_ATTEMPTS,
    PRIOR_CARRIES,
    PRIOR_TARGETS,
    FillConfig,
    distribution,
    fill,
    shares,
    team_volume,
)
from engine.projection import WEIGHT_KEY, StatInput, project
from engine.scoring import BOX_SCORE_WEIGHTS, Position, fantasy_points, standard_weights
from ingest.projection_job import _has_usage
from ingest.usage import TeamUsage, load_usage

BACKEND = Path(__file__).resolve().parents[1]
CACHE = BACKEND / ".cache" / "tuning"
PPR = standard_weights(1.0)
Filled = tuple[Position, dict[str, StatInput], float, float, dict[str, float]]
"""(position, filled inputs, fumble rate, actual points, actual stats) for one player-game."""
POSITIONS: tuple[Position, ...] = ("QB", "RB", "WR", "TE")


@dataclass
class GameInputs:
    game_id: str
    total: float
    spread_home: float
    home: str
    teams: dict[str, TeamUsage]
    actual: dict[int, float]
    """Fantasy points scored (PPR) by each player who played."""
    box: dict[int, dict[str, float]]
    fumbles: dict[int, float]


def load(season: int) -> list[GameInputs]:
    path = CACHE / f"{season}-v2.pkl"
    if path.exists():
        # Our own cache, written below from our own database.
        loaded: list[GameInputs] = pickle.loads(path.read_bytes())  # noqa: S301
        return loaded
    found = []
    with Session(get_engine()) as session:
        backtest = Backtest(session, "ppr", model_only=True)
        job = backtest._projections
        for game in backtest.games([season]):
            if game.spread_line is None or game.total_line is None:
                continue
            as_of = game.kickoff_utc - timedelta(minutes=1)
            teams = load_usage(session, game, as_of)
            actual = {}
            for row in session.scalars(select(ActualStats).where(ActualStats.game_id == game.id)):
                actual[row.player_id] = {s: float(getattr(row, s)) for s in BOX_SCORE_WEIGHTS}
            points, fumbles = {}, {}
            for usage in teams.values():
                for pid, player in usage.players.items():
                    if pid in actual:
                        points[pid] = fantasy_points(PPR, player.position, actual[pid])
                        fumbles[pid] = job._fumble_rate(pid, player.position, as_of)
            found.append(
                GameInputs(
                    game.id,
                    game.total_line,
                    game.spread_line,
                    game.home_abbr,
                    teams,
                    points,
                    actual,
                    fumbles,
                )
            )
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(found))
    return found


def score(games: list[GameInputs], config: FillConfig) -> dict[str, float]:
    """PPR MAE per position of the filled means, over players who played."""
    errors: dict[str, list[float]] = defaultdict(list)
    pay = PPR.model_dump()
    for g in games:
        for team, usage in g.teams.items():
            spread = g.spread_home if team == g.home else -g.spread_home
            volume = team_volume(usage.history, g.total, spread)
            longer = (usage.long_history, usage.long_players) if usage.long_history else None
            found = shares(usage.history, list(usage.players.values()), config, longer)
            for pid, player in usage.players.items():
                if pid not in g.actual or not _has_usage(player):
                    continue
                means = fill(
                    player,
                    found[pid],
                    volume,
                    set(FILLABLE[player.position]),
                    config,
                    efficiency=usage.long_players.get(pid),
                    opponent=usage.opponent.get(player.position, {}),
                )
                projected = sum(pay[WEIGHT_KEY[s]] * m for s, m in means.items())
                projected += pay["fumble_lost"] * g.fumbles[pid]
                errors[player.position].append(abs(projected - g.actual[pid]))
    return {p: sum(e) / len(e) for p, e in errors.items()}


def _filled_players(games: list[GameInputs], config: FillConfig) -> list[Filled]:
    """(position, filled inputs, fumble rate, actual points, actual stats) per player who
    played, with the shipped distribution shapes."""
    params = load_distribution_params(DEFAULT_PATH)
    found: list[Filled] = []
    for g in games:
        for team, usage in g.teams.items():
            spread = g.spread_home if team == g.home else -g.spread_home
            volume = team_volume(usage.history, g.total, spread)
            longer = (usage.long_history, usage.long_players) if usage.long_history else None
            share = shares(usage.history, list(usage.players.values()), config, longer)
            for pid, player in usage.players.items():
                if pid not in g.box or not _has_usage(player):
                    continue
                means = fill(
                    player,
                    share[pid],
                    volume,
                    set(FILLABLE[player.position]),
                    config,
                    efficiency=usage.long_players.get(pid),
                    opponent=usage.opponent.get(player.position, {}),
                )
                inputs = {}
                for stat, mean in means.items():
                    model = (
                        StatModel("poisson")
                        if stat == "anytime_td"
                        else params.model(stat, player.position) or StatModel("poisson")
                    )
                    inputs[stat] = StatInput(distribution(model, mean), 0, source="model")
                box = g.box[pid]
                actual = {
                    **box,
                    "anytime_td": box["rush_tds"]
                    + (0 if player.position == "QB" else box["rec_tds"]),
                }
                found.append((player.position, inputs, g.fumbles[pid], g.actual[pid], actual))
    return found


def fit_correlations(
    players: list[Filled],
) -> dict[Position, dict[tuple[str, str], float]]:
    """Correlation of the normal scores of each outcome's place in its filled distribution."""
    scores: dict[Position, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for position, inputs, _, _, actual in players:
        if set(inputs) != set(FILLABLE[position]):
            continue
        for stat, given in inputs.items():
            u = min(max(given.distribution.pit(actual[stat]), 1e-4), 1 - 1e-4)
            scores[position][stat].append(float(special.ndtri(u)))
    found: dict[Position, dict[tuple[str, str], float]] = {}
    for position, by_stat in scores.items():
        stats_ = list(FILLABLE[position])
        matrix = np.corrcoef(np.array([by_stat[s] for s in stats_]))
        found[position] = {
            (a, b): round(float(matrix[i, j]), 2)
            for i, a in enumerate(stats_)
            for j, b in enumerate(stats_)
            if i < j
        }
    return found


def coverage(
    players: list[Filled],
    correlations: Mapping[Position, Mapping[tuple[str, str], float]],
    cv: float,
) -> dict[str, tuple[float, float]]:
    """position -> (share below p20, share below p80) of actual points."""
    below: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    for position, inputs, fumbles, points, _ in players:
        result = project(
            position,
            PPR,
            inputs,
            fumbles,
            samples=2000,
            correlations=correlations,
            volume_cv=cv,
        )
        below[position].append((points < result.p20, points < result.p80))
    return {
        p: (sum(a for a, _ in v) / len(v), sum(b for _, b in v) / len(v)) for p, v in below.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--season", type=int, default=2023, help="the tuning season")
    parser.add_argument("--check", default="", help="held-out seasons to print MAE for")
    args = parser.parse_args()
    games = load(args.season)
    print(f"{len(games)} games in {args.season}")

    untuned = FillConfig(qb_starter=False)
    print("untuned:", _fmt(score(games, untuned)))
    longs = (0.0, 0.3, 0.5, 0.7, 0.85)
    results = []
    grid = itertools.product(
        (1.0, 2.0),  # prior multiplier
        (0.0, 0.25, 0.5),  # opponent_weight
        (4.0, 8.0),  # td_share_prior
        (0.5, 0.6, 0.7),  # qb_starter_share
    )
    for prior, opponent, td_prior, floor in grid:
        config = replace(
            untuned,
            qb_starter=True,
            prior_targets=PRIOR_TARGETS * prior,
            prior_carries=PRIOR_CARRIES * prior,
            prior_attempts=PRIOR_ATTEMPTS * prior,
            opponent_weight=opponent,
            td_share_prior=td_prior,
            qb_starter_share=floor,
        )
        # Each position takes the longer-window weight that suits it best.
        by_long = {
            w: score(games, replace(config, long_weight=dict.fromkeys(POSITIONS, w))) for w in longs
        }
        chosen: dict[str, float] = {p: min(longs, key=lambda w: by_long[w][p]) for p in POSITIONS}
        config = replace(config, long_weight=chosen)
        mae = score(games, config)
        results.append((sum(mae.values()) / len(mae), config, mae))
    results.sort(key=lambda r: r[0])
    for mean, config, mae in results[:5]:
        print(f"{mean:.4f}  {_describe(config)}  {_fmt(mae)}")
    tuned = results[0][1]

    players = _filled_players(games, tuned)
    correlations = fit_correlations(players)
    print("\nCorrelations:", correlations)
    print("independent ranges:", _cover(coverage(players, {}, 0.0)))
    print("correlated ranges:", _cover(coverage(players, correlations, 0.0)))
    best_cv, best_gap = 0.0, 9.0
    for cv in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6):
        found = coverage(players, correlations, cv)
        gap = sum(abs(lo - 0.2) + abs(hi - 0.8) for lo, hi in found.values())
        print(f"cv {cv}:", _cover(found))
        if gap < best_gap:
            best_cv, best_gap = cv, gap
    print(f"best volume cv: {best_cv}")
    print("\nTuned FillConfig:")
    print(f"  {_describe(tuned)}")
    for season in [int(s) for s in args.check.split(",") if s.strip()]:
        held = load(season)
        print(f"\n{season} (held out)")
        print("  untuned:", _fmt(score(held, untuned)))
        print("  tuned:", _fmt(score(held, tuned)))


def _describe(config: FillConfig) -> str:
    return (
        f"long_weight={dict(config.long_weight)}, "
        f"prior x{config.prior_targets / PRIOR_TARGETS:.0f}, "
        f"opponent_weight={config.opponent_weight}, td_share_prior={config.td_share_prior}, "
        f"qb_starter_share={config.qb_starter_share}"
    )


def _cover(found: dict[str, tuple[float, float]]) -> str:
    return "  ".join(
        f"{p} {found[p][0]:.0%}/{found[p][1]:.0%}" for p in ("QB", "RB", "WR", "TE") if p in found
    )


def _fmt(mae: dict[str, float]) -> str:
    return "  ".join(f"{p} {mae[p]:.3f}" for p in ("QB", "RB", "WR", "TE") if p in mae)


if __name__ == "__main__":
    main()
