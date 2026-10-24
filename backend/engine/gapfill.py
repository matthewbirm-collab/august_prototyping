"""Fill stats that have no prop market from team volume and usage shares. Pure.

Books post props for starters; backup running backs, third receivers and most quarterbacks'
rushing yards often have none. For those stats (CLAUDE.md rule 4):

    team implied points   total / 2 + spread / 2, from the game lines
    team volume           trailing pass attempts, carries and targets, moved by implied points
                          and the spread (favourites run more), and expected offensive TDs
    player share          trailing per-game share of targets, carries and pass attempts, with
                          injured players zeroed and their share handed to teammates
    player efficiency     yards per target / carry / attempt and catch rate, pulled toward the
                          position average while the sample is small

Only stats with no market are filled. The engine marks them `source="model"` and gives them
less credit in the confidence score. The league coefficients below were measured on 2021-2025
regular-season nflverse games (2,718 team-games) and are starting values for `/p3-backtest`.
"""

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from engine.distributions import FittedDistribution, StatModel
from engine.scoring import Position

# League averages and slopes, per team-game (2021-2025 regular seasons).
LEAGUE_IMPLIED_POINTS: Final = 22.3
PLAYS_PER_IMPLIED_POINT: Final = 0.359
"""Pass attempts plus carries gained per point of implied total."""
PASS_RATE_PER_IMPLIED_POINT: Final = 0.00685
PASS_RATE_PER_SPREAD_POINT: Final = -0.0055
"""Per point a team is favoured by: favourites run more."""
TD_INTERCEPT: Final = -0.868
TDS_PER_IMPLIED_POINT: Final = 0.1455
TDS_PER_SPREAD_POINT: Final = -0.005
MIN_TEAM_TDS: Final = 0.5
LEAGUE_PASS_RATE: Final = 0.553
"""Pass attempts over pass attempts plus carries."""
LEAGUE_PASS_TD_SHARE: Final = 0.615
PASS_TD_SHARE_PRIOR_TDS: Final = 8.0
"""Touchdowns of league-average pass/rush split blended into a team's own split."""
MIN_PASS_RATE, MAX_PASS_RATE = 0.35, 0.75

# Position averages: yards per target, catch rate, yards per carry; QB yards per attempt and
# interception rate.
YARDS_PER_TARGET: Final[Mapping[str, float]] = {"RB": 5.76, "WR": 7.92, "TE": 7.29, "QB": 5.0}
CATCH_RATE: Final[Mapping[str, float]] = {"RB": 0.779, "WR": 0.631, "TE": 0.708, "QB": 0.6}
YARDS_PER_CARRY: Final[Mapping[str, float]] = {"RB": 4.30, "WR": 5.78, "TE": 4.27, "QB": 4.39}
YARDS_PER_ATTEMPT: Final = 7.06
INTERCEPTION_RATE: Final = 0.0227
# How many opportunities of the position average are blended into a player's own rate.
PRIOR_TARGETS: Final = 30.0
PRIOR_CARRIES: Final = 50.0
PRIOR_ATTEMPTS: Final = 100.0
PRIOR_INTERCEPTION_ATTEMPTS: Final = 300.0
TD_SHARE_PRIOR_TDS: Final = 4.0
"""Team touchdowns' worth of usage share blended into a player's own touchdown share."""

PROMOTED_SHARE: Final = 0.75
"""A depth-chart starter whose higher-usage teammate at the position is out gets at least this
fraction of that teammate's share."""

# Chosen on 2023 by scripts/tune_gapfill.py.
TUNED_LONG_WEIGHT: Final[Mapping[str, float]] = {"QB": 0.0, "RB": 0.5, "WR": 0.7, "TE": 0.7}
"""The tuner chose 0.7 for quarterbacks too, but that made 2024 and 2025 QB MAE 3.6% and 1.6%
worse: a benched or new starter's longer history is the wrong guide. Setting it to 0 was decided
after seeing those seasons, so QB results there are not a clean hold-out."""
TUNED_TD_SHARE_PRIOR: Final = 8.0
TUNED_QB_STARTER_SHARE: Final = 0.7
TUNED_OPPONENT_WEIGHT: Final = 0.25


@dataclass(frozen=True)
class FillConfig:
    """The knobs `scripts/tune_gapfill.py` tunes. Defaults are the values it chose on 2023, with
    2024-2025 held out (docs/BACKTEST_MODEL_ONLY.md), except the quarterback long-window weight:
    see `TUNED_LONG_WEIGHT`."""

    long_weight: Mapping[str, float] = field(default_factory=lambda: dict(TUNED_LONG_WEIGHT))
    """position -> weight of the longer window's per-game share against the recent window's
    (0 when a position isn't listed)."""
    prior_targets: float = PRIOR_TARGETS
    prior_carries: float = PRIOR_CARRIES
    prior_attempts: float = PRIOR_ATTEMPTS
    prior_interception_attempts: float = PRIOR_INTERCEPTION_ATTEMPTS
    td_share_prior: float = TUNED_TD_SHARE_PRIOR
    qb_starter: bool = True
    """Give the starting quarterback at least `qb_starter_share` of the passes."""
    qb_starter_share: float = TUNED_QB_STARTER_SHARE
    opponent_weight: float = TUNED_OPPONENT_WEIGHT
    """How far to move filled yardage toward what the opponent has allowed: 0 ignores the
    opponent, 1 takes its ratio to the league average at face value."""


FILLABLE: Final[Mapping[Position, tuple[str, ...]]] = {
    "QB": ("pass_yds", "pass_tds", "interceptions", "rush_yds", "anytime_td"),
    "RB": ("rush_yds", "rec_yds", "receptions", "anytime_td"),
    "WR": ("rec_yds", "receptions", "anytime_td"),
    "TE": ("rec_yds", "receptions", "anytime_td"),
}


@dataclass(frozen=True)
class TeamHistory:
    """A team's offensive totals over its trailing games."""

    games: int
    pass_attempts: float
    rush_attempts: float
    targets: float
    pass_tds: float
    rush_tds: float
    rec_tds: float


@dataclass(frozen=True)
class TeamVolume:
    """What the team is expected to do in this game."""

    implied_points: float
    pass_attempts: float
    rush_attempts: float
    targets: float
    pass_tds: float
    rush_tds: float


@dataclass(frozen=True)
class PlayerHistory:
    """A player's totals over the team's trailing games, and his status for this one."""

    player_id: int
    position: Position
    games: int
    """Of the team's trailing games, how many he played."""
    targets: float = 0
    receptions: float = 0
    rec_yds: float = 0
    rec_tds: float = 0
    carries: float = 0
    rush_yds: float = 0
    rush_tds: float = 0
    pass_attempts: float = 0
    pass_yds: float = 0
    pass_tds: float = 0
    interceptions: float = 0
    available: bool = True
    """False when ruled Out or Doubtful, or on reserve."""
    depth_rank: int | None = None


@dataclass(frozen=True)
class Shares:
    targets: float
    carries: float
    pass_attempts: float
    rush_tds: float
    rec_tds: float


def implied_points(total: float, spread: float) -> float:
    """A team's implied points; `spread` is positive when this team is favoured."""
    return total / 2 + spread / 2


def team_volume(history: TeamHistory, total: float, spread: float) -> TeamVolume:
    """Trailing per-game volume moved to this game's implied points and spread."""
    if history.games <= 0:
        raise ValueError("a team needs at least one trailing game")
    implied = implied_points(total, spread)
    pass_attempts = history.pass_attempts / history.games
    rush_attempts = history.rush_attempts / history.games
    plays = pass_attempts + rush_attempts
    gap = implied - LEAGUE_IMPLIED_POINTS

    new_plays = max(plays + PLAYS_PER_IMPLIED_POINT * gap, 1.0)
    rate = pass_attempts / plays if plays > 0 else LEAGUE_PASS_RATE
    rate += PASS_RATE_PER_IMPLIED_POINT * gap + PASS_RATE_PER_SPREAD_POINT * spread
    rate = min(max(rate, MIN_PASS_RATE), MAX_PASS_RATE)
    new_pass = new_plays * rate
    targets_per_attempt = history.targets / history.pass_attempts if history.pass_attempts else 0.9

    tds = max(TD_INTERCEPT + TDS_PER_IMPLIED_POINT * implied + TDS_PER_SPREAD_POINT * spread, 0)
    tds = max(tds, MIN_TEAM_TDS)
    own = history.pass_tds + history.rush_tds
    pass_share = (history.pass_tds + PASS_TD_SHARE_PRIOR_TDS * LEAGUE_PASS_TD_SHARE) / (
        own + PASS_TD_SHARE_PRIOR_TDS
    )
    return TeamVolume(
        implied_points=implied,
        pass_attempts=new_pass,
        rush_attempts=new_plays - new_pass,
        targets=new_pass * targets_per_attempt,
        pass_tds=tds * pass_share,
        rush_tds=tds * (1 - pass_share),
    )


SHARE_KINDS: Final = ("targets", "carries", "pass_attempts", "rush_tds", "rec_tds")


def shares(
    team: TeamHistory,
    players: Sequence[PlayerHistory],
    config: FillConfig = FillConfig(),  # noqa: B008 - frozen, so a shared default is safe
    longer: tuple[TeamHistory, Mapping[int, PlayerHistory]] | None = None,
) -> dict[int, Shares]:
    """Each player's share of the team's targets, carries, pass attempts and touchdowns.

    Shares are per game played, so a player back from injury isn't diluted by the games he
    missed. With `longer` (the team and players over a longer window), the recent share is
    blended with the longer one by `config.long_weight`; a player who hasn't played recently
    takes the longer one. Unavailable players get nothing; what they leave goes to their
    teammates in proportion to their shares, and a depth-chart starter replacing a teammate who
    is out gets at least `PROMOTED_SHARE` of that teammate's share. The starting quarterback
    throws `config.qb_starter_share` of the passes. No kind of share sums above 1.
    """
    recent = {p.player_id: _raw_shares(team, p, config) for p in players}
    if longer is not None and any(config.long_weight.values()):
        long_team, long_players = longer
        for p in players:
            past = long_players.get(p.player_id)
            position_weight = config.long_weight.get(p.position, 0.0)
            if past is None or past.games <= 0 or position_weight <= 0:
                continue
            old = _raw_shares(long_team, past, config)
            weight = 1.0 if p.games <= 0 else position_weight
            recent[p.player_id] = {
                k: (1 - weight) * recent[p.player_id][k] + weight * old[k] for k in SHARE_KINDS
            }

    result: dict[int, dict[str, float]] = {p.player_id: {} for p in players}
    for kind in SHARE_KINDS:
        trailing = {p.player_id: recent[p.player_id][kind] for p in players}
        found = _redistribute(players, trailing)
        if kind == "pass_attempts" and config.qb_starter:
            found = _starter_throws(players, found, config.qb_starter_share)
        for player_id, value in found.items():
            result[player_id][kind] = value
    return {player_id: Shares(**values) for player_id, values in result.items()}


def _raw_shares(team: TeamHistory, p: PlayerHistory, config: FillConfig) -> dict[str, float]:
    per_game = team.games if team.games > 0 else 1

    def rate(player_total: float, team_total: float) -> float:
        if p.games <= 0 or team_total <= 0:
            return 0.0
        return (player_total / p.games) / (team_total / per_game)

    target = rate(p.targets, team.targets)
    carry = rate(p.carries, team.rush_attempts)
    prior = config.td_share_prior
    return {
        "targets": target,
        "carries": carry,
        "pass_attempts": rate(p.pass_attempts, team.pass_attempts),
        "rush_tds": _td_share(rate(p.rush_tds, team.rush_tds), carry, team.rush_tds, prior),
        "rec_tds": _td_share(rate(p.rec_tds, team.rec_tds), target, team.rec_tds, prior),
    }


def _td_share(
    own: float, usage: float, team_tds: float, prior: float = TD_SHARE_PRIOR_TDS
) -> float:
    """A player's touchdown share, pulled toward his usage share: a few touchdowns are noise.
    This stands in for red-zone share, which needs play-by-play data."""
    weight = team_tds / (team_tds + prior) if team_tds + prior > 0 else 0.0
    return weight * own + (1 - weight) * usage


def _starter_throws(
    players: Sequence[PlayerHistory], found: Mapping[int, float], starter_share: float
) -> dict[int, float]:
    """Passing isn't a committee: the starter throws at least `starter_share`.

    The starter is the available quarterback highest on the depth chart, or, with no chart, the
    one with the largest share. The others keep their shares, scaled down to what is left. A
    floor rather than winner-take-all, because the chart names the wrong starter about one game
    in ten (2023), and then a hedge costs far less.
    """
    quarterbacks = [p for p in players if p.position == "QB" and p.available]
    if not quarterbacks:
        return dict(found)
    starter = min(
        quarterbacks,
        key=lambda p: (p.depth_rank if p.depth_rank is not None else 99, -found[p.player_id]),
    )
    result = dict(found)
    others = [p for p in quarterbacks if p is not starter]
    # Trick plays: non-quarterbacks keep their (small) shares, and the total stays within 1.
    non_qb = sum(v for pid, v in found.items() if pid not in {p.player_id for p in quarterbacks})
    room = max(0.0, 1.0 - non_qb)
    result[starter.player_id] = min(max(found[starter.player_id], starter_share), room)
    left = room - result[starter.player_id]
    rest = sum(found[p.player_id] for p in others)
    for p in others:
        result[p.player_id] = (
            min(found[p.player_id], left * found[p.player_id] / rest) if rest > 1e-9 else 0.0
        )
    return result


def _redistribute(
    players: Sequence[PlayerHistory], trailing: Mapping[int, float]
) -> dict[int, float]:
    available = [p for p in players if p.available]
    out = [p for p in players if not p.available]
    shares = {p.player_id: trailing[p.player_id] for p in available}

    for position in {p.position for p in out}:
        vacated = max((trailing[p.player_id] for p in out if p.position == position), default=0.0)
        for p in available:
            if p.position == position and p.depth_rank == 1:
                shares[p.player_id] = max(shares[p.player_id], PROMOTED_SHARE * vacated)

    budget = min(1.0, sum(trailing.values()))
    held = sum(shares.values())
    # Below this there is nothing real to hand on; dividing by it would blow up.
    if held > 1e-9 and (held > budget or out):
        scale = budget / held
        shares = {k: v * scale for k, v in shares.items()}
    result = {k: min(max(v, 0.0), budget) for k, v in shares.items()}
    result.update({p.player_id: 0.0 for p in out})
    return result


def fill(
    player: PlayerHistory,
    share: Shares,
    volume: TeamVolume,
    missing: Collection[str],
    config: FillConfig = FillConfig(),  # noqa: B008 - frozen, so a shared default is safe
    *,
    efficiency: PlayerHistory | None = None,
    opponent: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """The mean of each `missing` stat the position can be filled for. Stats with a market are
    never touched: pass only the ones without.

    `efficiency` is the history to judge yards per play by (a longer window, when there is
    one). `opponent` maps "pass_yds", "rush_yds" and "rec_yds" to what this week's opponent has
    allowed the position against the league average (1.0 is average).
    """
    position = player.position
    eff = efficiency if efficiency is not None and efficiency.games > 0 else player
    targets = volume.targets * share.targets
    carries = volume.rush_attempts * share.carries
    attempts = volume.pass_attempts * share.pass_attempts

    def matchup(stat: str) -> float:
        ratio = (opponent or {}).get(stat, 1.0)
        return 1.0 + config.opponent_weight * (ratio - 1.0)

    means = {
        "rec_yds": targets
        * _shrunk(eff.rec_yds, eff.targets, YARDS_PER_TARGET[position], config.prior_targets)
        * matchup("rec_yds"),
        "receptions": targets
        * _shrunk(eff.receptions, eff.targets, CATCH_RATE[position], config.prior_targets),
        "rush_yds": carries
        * _shrunk(eff.rush_yds, eff.carries, YARDS_PER_CARRY[position], config.prior_carries)
        * matchup("rush_yds"),
        "pass_yds": attempts
        * _shrunk(eff.pass_yds, eff.pass_attempts, YARDS_PER_ATTEMPT, config.prior_attempts)
        * matchup("pass_yds"),
        "interceptions": attempts
        * _shrunk(
            eff.interceptions,
            eff.pass_attempts,
            INTERCEPTION_RATE,
            config.prior_interception_attempts,
        ),
        "pass_tds": volume.pass_tds * share.pass_attempts,
        # Rushing plus receiving touchdowns. A quarterback's are his rushing scores only (rule 2).
        "anytime_td": volume.rush_tds * share.rush_tds
        + (0.0 if position == "QB" else volume.pass_tds * share.rec_tds),
    }
    return {stat: max(means[stat], 0.0) for stat in FILLABLE[position] if stat in missing}


def _shrunk(total: float, opportunities: float, prior: float, prior_weight: float) -> float:
    return (total + prior * prior_weight) / (opportunities + prior_weight)


def distribution(model: StatModel, mean: float) -> FittedDistribution:
    """A distribution of the configured shape with this mean, for a filled stat.

    Yardage keeps the share of games at zero that the shape assigns to a line near the mean.
    """
    if mean <= 0:
        return FittedDistribution(StatModel("poisson"), 0.0)
    if model.family in {"poisson", "negbin", "bernoulli"}:
        if model.family == "negbin":
            return FittedDistribution(model, mean)
        return FittedDistribution(StatModel("poisson"), mean)
    zero = model.zero_inflation.at(mean) if model.zero_inflation else 0.0
    positive_mean = mean / (1 - zero)
    if model.shape is None:
        raise ValueError(f"{model.family} needs a shape")
    if model.family == "gamma":
        location = positive_mean / model.shape
    else:
        location = math.log(positive_mean) - model.shape**2 / 2
    return FittedDistribution(model, location, zero)
