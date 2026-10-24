"""Load what `engine/gapfill.py` needs for one game: each team's trailing volume, its players'
trailing usage, and who is out this week. Reads nothing dated after `as_of`."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.distribution_params import POSITION_ALIASES
from db.models import ActualStats, DepthChart, Game, Injury, Roster
from engine.gapfill import PlayerHistory, TeamHistory
from engine.scoring import Position

WINDOW_GAMES = 4
OUT_REPORT_STATUSES = frozenset({"Out", "Doubtful"})
OUT_ROSTER_STATUSES = frozenset({"RES", "INA", "DEV"})
"""Reserve (IR and the like), game-day inactive and practice squad."""
SKILL_POSITIONS: dict[str, Position] = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE"}


@dataclass
class TeamUsage:
    team: str
    history: TeamHistory
    players: dict[int, PlayerHistory] = field(default_factory=dict)


def skill_position(raw: str | None) -> Position | None:
    """The position the engine projects for, or None (kickers, linemen, unknown)."""
    raw = raw or ""
    return SKILL_POSITIONS.get(POSITION_ALIASES.get(raw, raw))


def load_usage(
    session: Session, game: Game, as_of: datetime, window: int = WINDOW_GAMES
) -> dict[str, TeamUsage]:
    """Usage for both teams of `game`, keyed by team. A team with no finished games in the
    window is left out."""
    roster = _roster(session, game, as_of)
    out = _out_this_week(session, game, as_of)
    depth = _depth(session, game, as_of)
    result: dict[str, TeamUsage] = {}
    for team in (game.home_abbr, game.away_abbr):
        games = _recent_games(session, team, game, as_of, window)
        if not games:
            continue
        totals = _player_totals(session, team, games)
        history = TeamHistory(
            games=len(games),
            pass_attempts=sum(t["pass_attempts"] for t in totals.values()),
            rush_attempts=sum(t["carries"] for t in totals.values()),
            targets=sum(t["targets"] for t in totals.values()),
            pass_tds=sum(t["pass_tds"] for t in totals.values()),
            rush_tds=sum(t["rush_tds"] for t in totals.values()),
            rec_tds=sum(t["rec_tds"] for t in totals.values()),
        )
        usage = TeamUsage(team, history)
        for player_id, (player_team, position, status) in roster.items():
            if player_team != team:
                continue
            own = totals.get(player_id, {})
            usage.players[player_id] = PlayerHistory(
                player_id=player_id,
                position=position,
                games=int(own.get("games", 0)),
                **{k: v for k, v in own.items() if k != "games"},
                available=status not in OUT_ROSTER_STATUSES and player_id not in out,
                depth_rank=depth.get((player_id, position)),
            )
        result[team] = usage
    return result


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
