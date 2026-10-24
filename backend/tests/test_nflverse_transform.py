"""Transform tests on recorded nflverse samples (ARI and HOU, 2026 weeks 1-6, plus 2024)."""

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import polars as pl
import pytest

from ingest.nflverse import transform
from ingest.nflverse.source import ParquetSource, cache_seconds

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "nflverse"
HARRISON = "00-0039849"  # Marvin Harrison Jr., ARI -> HOU in 2026


@pytest.fixture(scope="module")
def source() -> ParquetSource:
    return ParquetSource(FIXTURES)


@pytest.fixture(scope="module")
def kickoffs(source: ParquetSource) -> transform.KickoffIndex:
    games = transform.schedule_rows(source.schedules([2024, 2026]))
    return transform.KickoffIndex.build(
        (g["season"], g["week"], g["home_abbr"], g["away_abbr"], g["kickoff_utc"]) for g in games
    )


@pytest.fixture(scope="module")
def players(source: ParquetSource) -> dict[str, int]:
    """Fake player ids for every GSIS id in the samples."""
    ids = set(source.rosters_weekly([2026])["gsis_id"].drop_nulls())
    for frame, column in [
        (source.injuries([2024]), "gsis_id"),
        (source.depth_charts([2024]), "gsis_id"),
        (source.depth_charts([2026]), "gsis_id"),
    ]:
        ids |= set(frame[column].drop_nulls())
    return {gsis: i for i, gsis in enumerate(sorted(ids), start=1)}


# Schedules and timing --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gameday", "gametime", "expected"),
    [
        ("2026-09-13", "13:00", datetime(2026, 9, 13, 17, 0, tzinfo=UTC)),  # EDT
        ("2026-11-15", "13:00", datetime(2026, 11, 15, 18, 0, tzinfo=UTC)),  # EST
        ("2026-10-18", "09:30", datetime(2026, 10, 18, 13, 30, tzinfo=UTC)),  # London
        ("2026-12-27", None, datetime(2026, 12, 27, 18, 0, tzinfo=UTC)),  # TBD slot
    ],
)
def test_kickoffs_convert_from_eastern(
    gameday: str, gametime: str | None, expected: datetime
) -> None:
    assert transform.kickoff_utc(gameday, gametime) == expected


def test_schedule_rows(source: ParquetSource) -> None:
    games = {g["id"]: g for g in transform.schedule_rows(source.schedules([2024, 2026]))}
    assert len(games) == 557
    hou_jax = games["2026_06_HOU_JAX"]
    assert (hou_jax["home_abbr"], hou_jax["away_abbr"], hou_jax["week"]) == ("JAX", "HOU", 6)
    assert {g["season_type"] for g in games.values() if g["season"] == 2024} == {"REG", "POST"}


def test_kickoff_index(kickoffs: transform.KickoffIndex) -> None:
    week6 = kickoffs.team_kickoff(2026, 6, "HOU")
    assert week6 == datetime(2026, 10, 18, 13, 30, tzinfo=UTC)
    assert kickoffs.next_game("HOU", week6 - timedelta(minutes=1)) == (2026, 6)
    assert kickoffs.next_game("HOU", week6) == (2026, 7)
    # A Friday ranking belongs to the week whose games are still to come.
    assert kickoffs.week_in_progress(date(2026, 10, 23)) == (2026, 7)
    assert kickoffs.week_in_progress(date(2026, 9, 11)) == (2026, 1)
    # Off-season dates, and seasons whose schedule isn't loaded, belong to no week.
    assert kickoffs.week_in_progress(date(2026, 6, 1)) is None
    assert kickoffs.week_in_progress(date(2022, 10, 21)) is None


def test_cache_lifetime_depends_on_season() -> None:
    assert cache_seconds(datetime(2026, 10, 23, tzinfo=UTC)) == 6 * 3600
    assert cache_seconds(datetime(2026, 6, 1, tzinfo=UTC)) == 7 * 24 * 3600


# Players and rosters ---------------------------------------------------------------------------


def test_player_rows_take_latest_team_and_external_ids(source: ParquetSource) -> None:
    rows = {
        r["gsis_id"]: r
        for r in transform.player_rows(source.rosters_weekly([2026]), source.ff_playerids())
    }
    harrison = rows[HARRISON]
    assert harrison["full_name"] == "Marvin Harrison Jr."
    assert harrison["team_abbr"] == "HOU"
    assert {"sleeper", "pfr", "fantasypros", "espn"} <= set(harrison["external_ids"])
    assert len(rows) == source.rosters_weekly([2026])["gsis_id"].drop_nulls().n_unique()


def test_roster_rows_follow_a_trade(
    source: ParquetSource, players: dict[str, int], kickoffs: transform.KickoffIndex
) -> None:
    rows = transform.roster_rows(source.rosters_weekly([2026]), players, kickoffs)
    harrison = {r["week"]: r["team_abbr"] for r in rows if r["player_id"] == players[HARRISON]}
    assert harrison == {1: "ARI", 2: "ARI", 3: "ARI", 4: "ARI", 5: "ARI", 6: "HOU"}
    assert {r["status"] for r in rows} <= transform.ON_TEAM_STATUSES
    week6 = next(r for r in rows if r["player_id"] == players[HARRISON] and r["week"] == 6)
    assert week6["available_at"] == kickoffs.team_kickoff(2026, 6, "HOU") - transform.ROSTER_LEAD  # type: ignore[operator]


def test_roster_rows_are_unique_per_player_week(
    source: ParquetSource, players: dict[str, int], kickoffs: transform.KickoffIndex
) -> None:
    rows = transform.roster_rows(source.rosters_weekly([2026]), players, kickoffs)
    keys = [(r["season"], r["week"], r["player_id"]) for r in rows]
    assert len(keys) == len(set(keys))


# Injuries and depth charts ---------------------------------------------------------------------


def test_injuries_use_date_modified_when_present(
    source: ParquetSource, players: dict[str, int], kickoffs: transform.KickoffIndex
) -> None:
    rows = transform.injury_rows(source.injuries([2024]), players, kickoffs)
    assert rows
    assert all(r["available_at"].date() == date(2024, 9, 6) or r["week"] != 1 for r in rows)
    assert all(r["available_at"].tzinfo is not None for r in rows)


def test_injuries_without_date_modified_use_the_day_before_kickoff(
    source: ParquetSource, players: dict[str, int], kickoffs: transform.KickoffIndex
) -> None:
    rows = transform.injury_rows(source.injuries([2026]), players, kickoffs)
    assert rows
    for r in rows:
        kickoff = kickoffs.team_kickoff(2026, r["week"], r["team_abbr"])
        assert r["available_at"] == kickoff - transform.INJURY_LEAD  # type: ignore[operator]


def test_timestamped_depth_charts_keep_only_rank_changes(
    source: ParquetSource, players: dict[str, int], kickoffs: transform.KickoffIndex
) -> None:
    raw = source.depth_charts([2026])
    rows = transform.depth_chart_rows(raw, players, kickoffs)
    assert 0 < len(rows) < raw.height / 10

    by_key: dict[tuple[int, str], list[dict[str, object]]] = {}
    for r in sorted(rows, key=lambda r: r["available_at"]):
        by_key.setdefault((r["player_id"], r["position"]), []).append(r)
    for history in by_key.values():
        ranks = [h["depth_rank"] for h in history]
        assert all(a != b for a, b in pairwise(ranks))
    for r in rows:
        kickoff = kickoffs.team_kickoff(r["season"], r["week"], r["team_abbr"])
        assert kickoff is not None and r["available_at"] < kickoff


def test_weekly_depth_charts_map_positions_and_ranks(
    source: ParquetSource, players: dict[str, int], kickoffs: transform.KickoffIndex
) -> None:
    rows = transform.depth_chart_rows(source.depth_charts([2024]), players, kickoffs)
    assert rows
    assert {r["position"] for r in rows} <= {"QB", "RB", "WR", "TE", "FB"}
    assert {r["depth_rank"] for r in rows} <= {1, 2, 3}
    assert {r["season"] for r in rows} == {2024}


# Box scores, snaps and rankings ----------------------------------------------------------------


def test_actual_stats_sum_fumbles_and_two_point_conversions() -> None:
    stats = pl.DataFrame(
        {
            "player_id": ["g1"],
            "game_id": ["2026_06_HOU_JAX"],
            "season": [2026],
            "week": [6],
            "attempts": [30],
            "passing_interceptions": [1],
            "carries": [4],
            "sack_fumbles_lost": [1],
            "rushing_fumbles_lost": [1],
            "receiving_fumbles_lost": [0],
            "passing_2pt_conversions": [1],
            "rushing_2pt_conversions": [1],
        }
    )
    [row] = transform.actual_stat_rows(stats, {"g1": 7}, {"2026_06_HOU_JAX"})
    assert (row["player_id"], row["pass_attempts"], row["interceptions"]) == (7, 30, 1)
    assert (row["fumbles_lost"], row["two_pt_conversions"], row["receptions"]) == (2, 2, 0)


def test_actual_stats_from_samples(source: ParquetSource, players: dict[str, int]) -> None:
    game_ids = {g["id"] for g in transform.schedule_rows(source.schedules([2026]))}
    rows = transform.actual_stat_rows(source.player_stats([2026]), players, game_ids)
    harrison = {
        r["week"]: (r["receptions"], r["rec_yds"])
        for r in rows
        if r["player_id"] == players[HARRISON]
    }
    assert harrison[6] == (3, 34)
    assert 5 not in harrison  # inactive that week


def test_snap_counts_map_by_pfr_id(source: ParquetSource) -> None:
    snaps = source.snap_counts([2026])
    pfr_ids = {pfr: i for i, pfr in enumerate(snaps["pfr_player_id"].unique().to_list(), start=1)}
    game_ids = set(snaps["game_id"].unique().to_list())
    rows = transform.snap_count_rows(snaps, pfr_ids, game_ids)
    assert len(rows) == snaps.unique(subset=["game_id", "pfr_player_id"]).height
    assert all(0 <= r["offense_pct"] <= 1 for r in rows)


def test_rankings_assign_weeks_and_map_team_codes(kickoffs: transform.KickoffIndex) -> None:
    rankings = pl.DataFrame(
        {
            "ecr_type": ["wp", "wp", "wp", "ro"],
            "page_type": ["weekly-wr", "weekly-rb", "weekly-k", "redraft-overall"],
            "scrape_date": ["2026-10-16", "2026-10-23", "2026-10-23", "2026-10-23"],
            "id": ["101", "102", "103", "104"],
            "player": ["A", "B", "C", "D"],
            "pos": ["WR", "RB", "K", "WR"],
            "team": ["JAC", "FA", "ARI", "ARI"],
            "ecr": [3.5, 10.0, 1.0, 2.0],
            "sd": [1.0, 2.0, 0.5, 0.5],
            "best": [1, 5, 1, 1],
            "worst": [8, 20, 2, 3],
        }
    )
    rows = {
        r["fantasypros_id"]: r
        for r in transform.ranking_rows(rankings, {"101": 9}, kickoffs, {2026})
    }
    assert set(rows) == {101, 102}
    assert (rows[101]["week"], rows[101]["team_abbr"], rows[101]["player_id"]) == (6, "JAX", 9)
    assert (rows[102]["week"], rows[102]["team_abbr"], rows[102]["player_id"]) == (7, None, None)


def test_recorded_rankings_parse(source: ParquetSource, kickoffs: transform.KickoffIndex) -> None:
    rows = transform.ranking_rows(source.ff_rankings(), {}, kickoffs, {2026})
    assert rows
    assert {r["week"] for r in rows} <= set(range(1, 8))
    assert {r["page_type"] for r in rows} <= transform.RANKING_PAGES
