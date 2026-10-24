"""Turn nflverse DataFrames into table rows. Pure: no database or network.

`available_at` (when a fact became public, for no-leakage backtests) uses the source's own
timestamp where it has one: `date_modified` on injuries through 2024, `dt` on depth charts from
2025. Otherwise it's an approximation tied to that team's kickoff that week:

- rosters and pre-2025 depth charts: 3 days before kickoff
- injuries without `date_modified` (2025 on): 1 day before kickoff, after the final report
"""

import bisect
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

EASTERN = ZoneInfo("America/New_York")
ROSTER_LEAD = timedelta(days=3)
# Weekly rankings are published in the days before a week's games.
RANKING_WINDOW = timedelta(days=7)
INJURY_LEAD = timedelta(days=1)
# nflverse publishes kickoff times in US Eastern; TBD games get the Sunday early slot.
DEFAULT_GAMETIME = "13:00"

# Weekly roster statuses meaning the player is on the team that week. TRD, CUT, RET, EXE and
# similar rows describe someone who has left.
ON_TEAM_STATUSES = frozenset({"ACT", "RES", "INA", "DEV"})
DEPTH_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "FB"})
OLD_DEPTH_POSITIONS = {"QB": "QB", "RB": "RB", "HB": "RB", "WR": "WR", "TE": "TE", "FB": "FB"}
RANKING_PAGES = frozenset({"weekly-qb", "weekly-rb", "weekly-wr", "weekly-te"})
# FantasyPros team codes that differ from nflverse's. "FA" (free agent) has no team.
FANTASYPROS_TEAMS = {"JAC": "JAX", "LAR": "LA", "FA": None}
EXTERNAL_ID_COLUMNS = {
    "mfl_id": "mfl",
    "sportradar_id": "sportradar",
    "fantasypros_id": "fantasypros",
    "sleeper_id": "sleeper",
    "espn_id": "espn",
    "yahoo_id": "yahoo",
    "pfr_id": "pfr",
    "pff_id": "pff",
    "rotowire_id": "rotowire",
    "fantasy_data_id": "fantasy_data",
    "cbs_id": "cbs",
}

Row = dict[str, Any]


def kickoff_utc(gameday: str, gametime: str | None) -> datetime:
    local = datetime.fromisoformat(f"{gameday}T{gametime or DEFAULT_GAMETIME}")
    return local.replace(tzinfo=EASTERN).astimezone(UTC)


@dataclass
class KickoffIndex:
    """Kickoff time for each team's game each week, from the `games` table."""

    by_team_week: dict[tuple[int, int, str], datetime] = field(default_factory=dict)
    by_team: dict[str, list[tuple[datetime, int, int]]] = field(default_factory=dict)
    week_last_kickoff: list[tuple[datetime, int, int]] = field(default_factory=list)
    week_first_kickoff: dict[tuple[int, int], datetime] = field(default_factory=dict)

    @classmethod
    def build(cls, games: Iterable[tuple[int, int, str, str, datetime]]) -> "KickoffIndex":
        index = cls()
        last: dict[tuple[int, int], datetime] = {}
        first = index.week_first_kickoff
        for season, week, home, away, kickoff in games:
            for team in (home, away):
                index.by_team_week[(season, week, team)] = kickoff
                index.by_team.setdefault(team, []).append((kickoff, season, week))
            last[(season, week)] = max(kickoff, last.get((season, week), kickoff))
            first[(season, week)] = min(kickoff, first.get((season, week), kickoff))
        for games_by_team in index.by_team.values():
            games_by_team.sort()
        index.week_last_kickoff = sorted((k, s, w) for (s, w), k in last.items())
        return index

    def team_kickoff(self, season: int, week: int, team: str) -> datetime | None:
        return self.by_team_week.get((season, week, team))

    def next_game(self, team: str, moment: datetime) -> tuple[int, int] | None:
        """(season, week) of the team's first game kicking off after `moment`."""
        games = self.by_team.get(team, [])
        i = bisect.bisect_right(games, (moment, 10**6, 10**6))
        return (games[i][1], games[i][2]) if i < len(games) else None

    def week_in_progress(self, day: date) -> tuple[int, int] | None:
        """(season, week) of the first week whose last game is on or after `day`, if `day` is
        within `RANKING_WINDOW` of that week's first kickoff. Dates outside any loaded season
        (the off-season, or seasons whose schedule isn't loaded) give None.
        """
        moment = datetime(day.year, day.month, day.day, tzinfo=UTC)
        i = bisect.bisect_left(self.week_last_kickoff, (moment, 0, 0))
        if i == len(self.week_last_kickoff):
            return None
        _, season, week = self.week_last_kickoff[i]
        if moment < self.week_first_kickoff[(season, week)] - RANKING_WINDOW:
            return None
        return season, week


def schedule_rows(schedules: pl.DataFrame) -> list[Row]:
    return [
        {
            "id": r["game_id"],
            "season": r["season"],
            "week": r["week"],
            "season_type": "REG" if r["game_type"] == "REG" else "POST",
            "kickoff_utc": kickoff_utc(r["gameday"], r["gametime"]),
            "home_abbr": r["home_team"],
            "away_abbr": r["away_team"],
            # nflverse convention: positive means the home team is favored.
            "spread_line": r["spread_line"],
            "total_line": r["total_line"],
        }
        for r in schedules.iter_rows(named=True)
    ]


def player_rows(rosters: pl.DataFrame, ff_playerids: pl.DataFrame) -> list[Row]:
    """One row per GSIS id on the rosters, latest week first, enriched from ff_playerids."""
    ids_by_gsis = {
        r["gsis_id"]: r
        for r in ff_playerids.filter(pl.col("gsis_id").is_not_null()).iter_rows(named=True)
    }
    latest = (
        rosters.filter(pl.col("gsis_id").is_not_null())
        .sort(["season", "week"], descending=True)
        .unique(subset="gsis_id", keep="first", maintain_order=True)
    )
    rows = []
    for r in latest.iter_rows(named=True):
        external: dict[str, str] = {}
        ff = ids_by_gsis.get(r["gsis_id"], {})
        for column, key in EXTERNAL_ID_COLUMNS.items():
            value = ff.get(column) if ff.get(column) is not None else r.get(column)
            if value is not None:
                external[key] = str(value)
        rows.append(
            {
                "gsis_id": r["gsis_id"],
                "full_name": r["full_name"],
                "position": r["position"],
                "team_abbr": r["team"] if r["status"] in ON_TEAM_STATUSES else None,
                "birth_date": r["birth_date"],
                "external_ids": external,
            }
        )
    return rows


def roster_rows(
    rosters: pl.DataFrame, players: Mapping[str, int], kickoffs: KickoffIndex
) -> list[Row]:
    rows: dict[tuple[int, int, int], Row] = {}
    for r in rosters.filter(pl.col("status").is_in(list(ON_TEAM_STATUSES))).iter_rows(named=True):
        player_id = players.get(r["gsis_id"])
        kickoff = kickoffs.team_kickoff(r["season"], r["week"], r["team"])
        if player_id is None or kickoff is None:
            continue  # unknown player, or the team's bye week
        key = (r["season"], r["week"], player_id)
        if key in rows and rows[key]["status"] == "ACT":
            continue
        rows[key] = {
            "season": r["season"],
            "week": r["week"],
            "player_id": player_id,
            "team_abbr": r["team"],
            "position": r["position"],
            "status": r["status"],
            "available_at": kickoff - ROSTER_LEAD,
        }
    return list(rows.values())


def injury_rows(
    injuries: pl.DataFrame, players: Mapping[str, int], kickoffs: KickoffIndex
) -> list[Row]:
    has_modified = "date_modified" in injuries.columns
    rows: dict[tuple[int, int, int, datetime], Row] = {}
    for r in injuries.iter_rows(named=True):
        player_id = players.get(r["gsis_id"])
        if player_id is None:
            continue
        modified = r.get("date_modified") if has_modified else None
        if modified:
            available_at = _parse_utc(modified)
        else:
            kickoff = kickoffs.team_kickoff(r["season"], r["week"], r["team"])
            if kickoff is None:
                continue
            available_at = kickoff - INJURY_LEAD
        key = (r["season"], r["week"], player_id, available_at)
        rows[key] = {
            "season": r["season"],
            "week": r["week"],
            "player_id": player_id,
            "available_at": available_at,
            "team_abbr": r["team"],
            "report_status": r["report_status"],
            "practice_status": r["practice_status"],
            "primary_injury": r["report_primary_injury"] or r["practice_primary_injury"],
        }
    return list(rows.values())


def depth_chart_rows(
    depth: pl.DataFrame, players: Mapping[str, int], kickoffs: KickoffIndex
) -> list[Row]:
    if depth.is_empty():
        return []
    if "dt" in depth.columns:
        return _depth_rows_timestamped(depth, players, kickoffs)
    return _depth_rows_weekly(depth, players, kickoffs)


def _depth_rows_timestamped(
    depth: pl.DataFrame, players: Mapping[str, int], kickoffs: KickoffIndex
) -> list[Row]:
    """2025+: many snapshots a day. Keep a row only when a player's rank at a position changes."""
    key = ["team", "gsis_id", "pos_abb"]
    changes = (
        depth.filter(
            pl.col("pos_abb").is_in(list(DEPTH_POSITIONS)), pl.col("gsis_id").is_not_null()
        )
        .sort([*key, "dt", "pos_rank"])
        .unique(subset=[*key, "dt"], keep="first", maintain_order=True)
        .filter(
            pl.col("pos_rank").shift(1).over(key).is_null()
            | (pl.col("pos_rank") != pl.col("pos_rank").shift(1).over(key))
        )
    )
    rows = []
    for r in changes.iter_rows(named=True):
        player_id = players.get(r["gsis_id"])
        moment = _parse_utc(r["dt"])
        game = kickoffs.next_game(r["team"], moment)
        if player_id is None or game is None:
            continue
        rows.append(
            {
                "season": game[0],
                "week": game[1],
                "player_id": player_id,
                "position": r["pos_abb"],
                "available_at": moment,
                "team_abbr": r["team"],
                "depth_rank": r["pos_rank"],
            }
        )
    return rows


def _depth_rows_weekly(
    depth: pl.DataFrame, players: Mapping[str, int], kickoffs: KickoffIndex
) -> list[Row]:
    """Through 2024: one chart per team per week; depth_team is the rank."""
    rows: dict[tuple[int, int, int, str], Row] = {}
    offense = depth.filter(pl.col("formation") == "Offense")
    for r in offense.iter_rows(named=True):
        position = OLD_DEPTH_POSITIONS.get((r["depth_position"] or "").strip())
        player_id = players.get(r["gsis_id"])
        kickoff = kickoffs.team_kickoff(r["season"], r["week"], r["club_code"])
        if position is None or player_id is None or kickoff is None:
            continue
        rank = int(r["depth_team"])
        key = (r["season"], r["week"], player_id, position)
        if key in rows and rows[key]["depth_rank"] <= rank:
            continue
        rows[key] = {
            "season": r["season"],
            "week": r["week"],
            "player_id": player_id,
            "position": position,
            "available_at": kickoff - ROSTER_LEAD,
            "team_abbr": r["club_code"],
            "depth_rank": rank,
        }
    return list(rows.values())


def snap_count_rows(
    snaps: pl.DataFrame, players_by_pfr: Mapping[str, int], game_ids: set[str]
) -> list[Row]:
    rows: dict[tuple[str, int], Row] = {}
    for r in snaps.iter_rows(named=True):
        player_id = players_by_pfr.get(r["pfr_player_id"])
        if player_id is None or r["game_id"] not in game_ids:
            continue
        rows[(r["game_id"], player_id)] = {
            "game_id": r["game_id"],
            "player_id": player_id,
            "season": r["season"],
            "week": r["week"],
            "team_abbr": r["team"],
            "position": r["position"],
            "offense_snaps": int(r["offense_snaps"] or 0),
            "offense_pct": float(r["offense_pct"] or 0),
            "defense_snaps": int(r["defense_snaps"] or 0),
            "defense_pct": float(r["defense_pct"] or 0),
            "st_snaps": int(r["st_snaps"] or 0),
            "st_pct": float(r["st_pct"] or 0),
        }
    return list(rows.values())


def actual_stat_rows(
    stats: pl.DataFrame, players: Mapping[str, int], game_ids: set[str]
) -> list[Row]:
    def total(r: Row, *columns: str) -> int:
        return sum(int(r.get(c) or 0) for c in columns)

    rows = []
    for r in stats.iter_rows(named=True):
        player_id = players.get(r["player_id"])
        if player_id is None or r["game_id"] not in game_ids:
            continue
        rows.append(
            {
                "game_id": r["game_id"],
                "player_id": player_id,
                "season": r["season"],
                "week": r["week"],
                "completions": total(r, "completions"),
                "pass_attempts": total(r, "attempts"),
                "pass_yds": total(r, "passing_yards"),
                "pass_tds": total(r, "passing_tds"),
                "interceptions": total(r, "passing_interceptions"),
                "rush_attempts": total(r, "carries"),
                "rush_yds": total(r, "rushing_yards"),
                "rush_tds": total(r, "rushing_tds"),
                "targets": total(r, "targets"),
                "receptions": total(r, "receptions"),
                "rec_yds": total(r, "receiving_yards"),
                "rec_tds": total(r, "receiving_tds"),
                "fumbles_lost": total(
                    r, "sack_fumbles_lost", "rushing_fumbles_lost", "receiving_fumbles_lost"
                ),
                "two_pt_conversions": total(
                    r,
                    "passing_2pt_conversions",
                    "rushing_2pt_conversions",
                    "receiving_2pt_conversions",
                ),
            }
        )
    return rows


def ranking_rows(
    rankings: pl.DataFrame,
    players_by_fantasypros: Mapping[str, int],
    kickoffs: KickoffIndex,
    seasons: set[int],
) -> list[Row]:
    """Weekly positional ECR (QB/RB/WR/TE), assigned to the week in progress on scrape day."""
    weekly = rankings.filter(
        pl.col("ecr_type") == "wp", pl.col("page_type").is_in(list(RANKING_PAGES))
    )
    rows: dict[tuple[date, str, int], Row] = {}
    for r in weekly.iter_rows(named=True):
        scraped = date.fromisoformat(r["scrape_date"])
        week = kickoffs.week_in_progress(scraped)
        if week is None or week[0] not in seasons or r["ecr"] is None:
            continue
        fantasypros_id = int(r["id"])
        team = r["team"]
        rows[(scraped, r["page_type"], fantasypros_id)] = {
            "season": week[0],
            "week": week[1],
            "scrape_date": scraped,
            "page_type": r["page_type"],
            "fantasypros_id": fantasypros_id,
            "player_id": players_by_fantasypros.get(str(fantasypros_id)),
            "player_name": r["player"],
            "position": r["pos"],
            "team_abbr": FANTASYPROS_TEAMS.get(team, team) if team else None,
            "ecr": r["ecr"],
            "sd": r["sd"],
            "best": _int_or_none(r["best"]),
            "worst": _int_or_none(r["worst"]),
        }
    return list(rows.values())


def _parse_utc(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)
