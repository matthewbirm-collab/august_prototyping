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
from dataclasses import dataclass
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


def shares(team: TeamHistory, players: Sequence[PlayerHistory]) -> dict[int, Shares]:
    """Each player's share of the team's targets, carries, pass attempts and touchdowns.

    Shares are per game played, so a player back from injury isn't diluted by the games he
    missed. Unavailable players get nothing; what they leave goes to their teammates in
    proportion to their shares, and a depth-chart starter replacing a teammate who is out gets
    at least `PROMOTED_SHARE` of that teammate's share. No kind of share sums above 1.
    """
    per_game = team.games if team.games > 0 else 1

    def rate(player_total: float, player: PlayerHistory, team_total: float) -> float:
        if player.games <= 0 or team_total <= 0:
            return 0.0
        return (player_total / player.games) / (team_total / per_game)

    raw: dict[int, dict[str, float]] = {}
    for p in players:
        target = rate(p.targets, p, team.targets)
        carry = rate(p.carries, p, team.rush_attempts)
        raw[p.player_id] = {
            "targets": target,
            "carries": carry,
            "pass_attempts": rate(p.pass_attempts, p, team.pass_attempts),
            "rush_tds": _td_share(rate(p.rush_tds, p, team.rush_tds), carry, team.rush_tds),
            "rec_tds": _td_share(rate(p.rec_tds, p, team.rec_tds), target, team.rec_tds),
        }

    result: dict[int, dict[str, float]] = {p.player_id: {} for p in players}
    for kind in ("targets", "carries", "pass_attempts", "rush_tds", "rec_tds"):
        trailing = {p.player_id: raw[p.player_id][kind] for p in players}
        for player_id, value in _redistribute(players, trailing).items():
            result[player_id][kind] = value
    return {player_id: Shares(**values) for player_id, values in result.items()}


def _td_share(own: float, usage: float, team_tds: float) -> float:
    """A player's touchdown share, pulled toward his usage share: a few touchdowns are noise.
    This stands in for red-zone share, which needs play-by-play data."""
    weight = team_tds / (team_tds + TD_SHARE_PRIOR_TDS)
    return weight * own + (1 - weight) * usage


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
) -> dict[str, float]:
    """The mean of each `missing` stat the position can be filled for. Stats with a market are
    never touched: pass only the ones without."""
    position = player.position
    targets = volume.targets * share.targets
    carries = volume.rush_attempts * share.carries
    attempts = volume.pass_attempts * share.pass_attempts

    means = {
        "rec_yds": targets
        * _shrunk(player.rec_yds, player.targets, YARDS_PER_TARGET[position], PRIOR_TARGETS),
        "receptions": targets
        * _shrunk(player.receptions, player.targets, CATCH_RATE[position], PRIOR_TARGETS),
        "rush_yds": carries
        * _shrunk(player.rush_yds, player.carries, YARDS_PER_CARRY[position], PRIOR_CARRIES),
        "pass_yds": attempts
        * _shrunk(player.pass_yds, player.pass_attempts, YARDS_PER_ATTEMPT, PRIOR_ATTEMPTS),
        "interceptions": attempts
        * _shrunk(
            player.interceptions,
            player.pass_attempts,
            INTERCEPTION_RATE,
            PRIOR_INTERCEPTION_ATTEMPTS,
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
