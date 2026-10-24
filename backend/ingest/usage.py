"""Load what `engine/gapfill.py` needs for one game: each team's trailing volume, its players'
trailing usage, and who is out this week. Reads nothing dated after `as_of`."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.distribution_params import POSITION_ALIASES
from db.models import ActualStats, DepthChart, Game, Injury, Roster
from engine.gapfill import PlayerHistory, TeamHistory
from engine.scoring import Position

WINDOW_GAMES = 4
LONG_WINDOW_GAMES = 12
"""The longer window recent shares are blended with, and efficiency is judged on."""
OPPONENT_GAMES = 8
# Yards a team gives up to each position per game, 2021-2022 regular seasons (before any season
# the backtest grades), for the opponent adjustment.
LEAGUE_ALLOWED: dict[str, dict[str, float]] = {
    "QB": {"pass_yds": 238.4, "rush_yds": 17.8},
    "RB": {"rush_yds": 94.0, "rec_yds": 35.0},
    "WR": {"rec_yds": 153.0},
    "TE": {"rec_yds": 49.9},
}
OUT_REPORT_STATUSES = frozenset({"Out", "Doubtful"})
OUT_ROSTER_STATUSES = frozenset({"RES", "DEV"})
"""Reserve (IR and the like) and practice squad."""
INACTIVE_STATUS = "INA"
INACTIVES_LEAD = timedelta(minutes=90)
"""Teams name their game-day inactives 90 minutes before kickoff. The weekly roster row carrying
`INA` is dated days earlier, so it only counts once `as_of` reaches that point."""
SKILL_POSITIONS: dict[str, Position] = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE"}


@dataclass
class TeamUsage:
    team: str
    history: TeamHistory
    players: dict[int, PlayerHistory] = field(default_factory=dict)
    long_history: TeamHistory | None = None
    long_players: dict[int, PlayerHistory] = field(default_factory=dict)
    """The same over `LONG_WINDOW_GAMES`."""
    opponent: dict[str, dict[str, float]] = field(default_factory=dict)
    """position -> stat -> yards this week's opponent allowed per game over the league
    average (1.0 is average). Empty when the opponent has no finished games."""


def skill_position(raw: str | None) -> Position | None:
    """The position the engine projects for, or None (kickers, linemen, unknown)."""
    raw = raw or ""
    return SKILL_POSITIONS.get(POSITION_ALIASES.get(raw, raw))


def load_usage(
    session: Session,
    game: Game,
    as_of: datetime,
    window: int = WINDOW_GAMES,
    long_window: int = LONG_WINDOW_GAMES,
) -> dict[str, TeamUsage]:
    """Usage for both teams of `game`, keyed by team. A team with no finished games in the
    window is left out."""
    roster = _roster(session, game, as_of)
    inactives_known = as_of >= game.kickoff_utc - INACTIVES_LEAD
    out = _out_this_week(session, game, as_of)
    depth = _depth(session, game, as_of)
    result: dict[str, TeamUsage] = {}
    for team in (game.home_abbr, game.away_abbr):
        long_games = _recent_games(session, team, game, as_of, max(window, long_window))
        games = long_games[:window]
        if not games:
            continue
        long_totals = _player_totals(session, team, long_games)
        totals = _player_totals(session, team, games) if len(long_games) > window else long_totals
        usage = TeamUsage(team, _team_history(len(games), totals))
        usage.long_history = _team_history(len(long_games), long_totals)
        for player_id, (player_team, position, status) in roster.items():
            if player_team != team:
                continue
            available = (
                status not in OUT_ROSTER_STATUSES
                and not (inactives_known and status == INACTIVE_STATUS)
                and player_id not in out
            )
            rank = depth.get((player_id, position))
            usage.players[player_id] = _player(player_id, position, totals, available, rank)
            usage.long_players[player_id] = _player(
                player_id, position, long_totals, available, rank
            )
        opponent = game.away_abbr if team == game.home_abbr else game.home_abbr
        usage.opponent = _allowed(session, opponent, game, as_of)
        result[team] = usage
    return result


def _team_history(games: int, totals: dict[int, dict[str, float]]) -> TeamHistory:
    return TeamHistory(
        games=games,
        pass_attempts=sum(t["pass_attempts"] for t in totals.values()),
        rush_attempts=sum(t["carries"] for t in totals.values()),
        targets=sum(t["targets"] for t in totals.values()),
        pass_tds=sum(t["pass_tds"] for t in totals.values()),
        rush_tds=sum(t["rush_tds"] for t in totals.values()),
        rec_tds=sum(t["rec_tds"] for t in totals.values()),
    )


def _player(
    player_id: int,
    position: Position,
    totals: dict[int, dict[str, float]],
    available: bool,
    rank: int | None,
) -> PlayerHistory:
    own = totals.get(player_id, {})
    return PlayerHistory(
        player_id=player_id,
        position=position,
        games=int(own.get("games", 0)),
        **{k: v for k, v in own.items() if k != "games"},
        available=available,
        depth_rank=rank,
    )


def _allowed(
    session: Session, defense: str, game: Game, as_of: datetime
) -> dict[str, dict[str, float]]:
    """What `defense` gave up to each position per game over its last games before `as_of`,
    as a ratio to the league average."""
    games = _recent_games(session, defense, game, as_of, OPPONENT_GAMES)
    if not games:
        return {}
    stmt = (
        select(Roster.position, ActualStats.pass_yds, ActualStats.rush_yds, ActualStats.rec_yds)
        .join(
            Roster,
            (Roster.player_id == ActualStats.player_id)
            & (Roster.season == ActualStats.season)
            & (Roster.week == ActualStats.week),
        )
        .where(ActualStats.game_id.in_(games), Roster.team_abbr != defense)
    )
    sums: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for raw, pass_yds, rush_yds, rec_yds in session.execute(stmt):
        position = skill_position(raw)
        if position is None:
            continue
        sums[position]["pass_yds"] += pass_yds
        sums[position]["rush_yds"] += rush_yds
        sums[position]["rec_yds"] += rec_yds
    return {
        position: {
            stat: sums[position][stat] / len(games) / average for stat, average in stats.items()
        }
        for position, stats in LEAGUE_ALLOWED.items()
    }


def _roster(
    session: Session, game: Game, as_of: datetime
) -> dict[int, tuple[str, Position, str | None]]:
    """player -> (team, position, roster status) for the game's week, as known at `as_of`."""
    rows = session.execute(
        select(Roster.player_id, Roster.team_abbr, Roster.position, Roster.status).where(
            Roster.season == game.season,
            Roster.week == game.week,
            Roster.team_abbr.in_([game.home_abbr, game.away_abbr]),
            Roster.available_at <= as_of,
        )
    )
    found: dict[int, tuple[str, Position, str | None]] = {}
    for player_id, team, raw, status in rows:
        position = skill_position(raw)
        if position is not None:
            found[player_id] = (team, position, status)
    return found


def _out_this_week(session: Session, game: Game, as_of: datetime) -> set[int]:
    """Players whose latest injury report before `as_of` says Out or Doubtful."""
    stmt = (
        select(Injury.player_id, Injury.report_status)
        .where(
            Injury.season == game.season,
            Injury.week == game.week,
            Injury.available_at <= as_of,
        )
        .ext(distinct_on(Injury.player_id))
        .order_by(Injury.player_id, Injury.available_at.desc())
    )
    return {p for p, status in session.execute(stmt) if status in OUT_REPORT_STATUSES}


def _depth(session: Session, game: Game, as_of: datetime) -> dict[tuple[int, str], int]:
    """(player, position) -> depth-chart rank in the latest chart before `as_of`."""
    stmt = (
        select(DepthChart.player_id, DepthChart.position, DepthChart.depth_rank)
        .where(
            DepthChart.season == game.season,
            DepthChart.week == game.week,
            DepthChart.available_at <= as_of,
        )
        .ext(distinct_on(DepthChart.player_id, DepthChart.position))
        .order_by(DepthChart.player_id, DepthChart.position, DepthChart.available_at.desc())
    )
    ranks: dict[tuple[int, str], int] = {}
    for player_id, raw, rank in session.execute(stmt):
        position = skill_position(raw)
        if position is not None:
            key = (player_id, position)
            ranks[key] = min(rank, ranks.get(key, rank))
    return ranks


def _recent_games(
    session: Session, team: str, game: Game, as_of: datetime, window: int
) -> list[str]:
    """The team's last `window` games that kicked off before `as_of`, this one excluded."""
    return list(
        session.scalars(
            select(Game.id)
            .where(
                or_(Game.home_abbr == team, Game.away_abbr == team),
                Game.kickoff_utc < as_of,
                Game.id != game.id,
            )
            .order_by(Game.kickoff_utc.desc())
            .limit(window)
        )
    )


def _player_totals(session: Session, team: str, games: list[str]) -> dict[int, dict[str, float]]:
    """Each player's totals in those games, counting only weeks he was on this team."""
    stmt = (
        select(ActualStats)
        .join(
            Roster,
            (Roster.player_id == ActualStats.player_id)
            & (Roster.season == ActualStats.season)
            & (Roster.week == ActualStats.week),
        )
        .where(ActualStats.game_id.in_(games), Roster.team_abbr == team)
    )
    totals: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in session.scalars(stmt):
        t = totals[row.player_id]
        t["games"] += 1
        t["targets"] += row.targets
        t["receptions"] += row.receptions
        t["rec_yds"] += row.rec_yds
        t["rec_tds"] += row.rec_tds
        t["carries"] += row.rush_attempts
        t["rush_yds"] += row.rush_yds
        t["rush_tds"] += row.rush_tds
        t["pass_attempts"] += row.pass_attempts
        t["pass_yds"] += row.pass_yds
        t["pass_tds"] += row.pass_tds
        t["interceptions"] += row.interceptions
    return {player: dict(values) for player, values in totals.items()}
