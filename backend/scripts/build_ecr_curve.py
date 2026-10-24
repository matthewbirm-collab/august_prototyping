"""Fit the ECR rank-to-points curves the backtest baseline uses.

    cd backend && uv run python scripts/build_ecr_curve.py

For each graded season (2023 onward) and each built-in scoring config, fits the mean actual
fantasy points at each weekly positional ECR rank on the seasons before it, from 2020. Writes
backend/config/ecr_points_curve.json. Downloads public nflverse data (CC-BY 4.0; FantasyPros
rankings via nflverse) through nflreadpy; needs no API key.
"""

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path

import nflreadpy as nfl
import polars as pl
from nflreadpy.config import update_config

from analytics.ecr import MAX_RANK, fit_curve, position_ranks
from db.seed_data import SCORING_CONFIGS
from engine.scoring import Position, fantasy_points
from ingest.nflverse.transform import RANKING_PAGES, KickoffIndex

BACKEND = Path(__file__).resolve().parents[1]
POSITIONS: dict[str, Position] = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE"}
STAT_COLUMNS = {
    "pass_yds": "passing_yards",
    "pass_tds": "passing_tds",
    "interceptions": "passing_interceptions",
    "rush_yds": "rushing_yards",
    "rec_yds": "receiving_yards",
    "receptions": "receptions",
    "rush_tds": "rushing_tds",
    "rec_tds": "receiving_tds",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--first-season", type=int, default=2020)
    parser.add_argument("--graded", default="2023,2024,2025,2026")
    parser.add_argument("--out", type=Path, default=BACKEND / "config" / "ecr_points_curve.json")
    args = parser.parse_args()
    graded = [int(s) for s in args.graded.split(",")]

    update_config(cache_mode="filesystem", cache_dir=BACKEND / ".cache" / "nflverse")
    seasons = list(range(args.first_season, max(graded)))
    schedule = nfl.load_schedules(seasons).filter(pl.col("game_type") == "REG")
    kickoffs = KickoffIndex.build(
        (r["season"], r["week"], r["home_team"], r["away_team"], _kickoff(r))
        for r in schedule.iter_rows(named=True)
    )
    stats = nfl.load_player_stats(seasons).filter(pl.col("season_type") == "REG")
    lines = {(r["player_id"], r["season"], r["week"]): r for r in stats.iter_rows(named=True)}
    ids = {
        str(r["fantasypros_id"]): r["gsis_id"]
        for r in nfl.load_ff_playerids().iter_rows(named=True)
        if r["fantasypros_id"] is not None and r["gsis_id"]
    }
    rankings = nfl.load_ff_rankings(type="all").filter(
        pl.col("ecr_type") == "wp", pl.col("page_type").is_in(list(RANKING_PAGES))
    )

    # (season, week, position) -> the latest scrape's ranks, keyed by gsis id
    latest: dict[tuple[int, int, str], tuple[date, dict[str, float]]] = {}
    for r in rankings.iter_rows(named=True):
        scraped = date.fromisoformat(r["scrape_date"])
        week = kickoffs.week_in_progress(scraped)
        gsis = ids.get(str(r["id"]))
        if week is None or gsis is None or r["ecr"] is None:
            continue
        key = (week[0], week[1], r["pos"])
        found = latest.get(key)
        if found is None or found[0] < scraped:
            latest[key] = found = (scraped, {})
        if found[0] == scraped:
            found[1][gsis] = float(r["ecr"])

    # (season, position, scoring) -> [(rank, points)]
    pairs: dict[tuple[int, str, str], list[tuple[int, float]]] = {}
    for (season, week_number, position), (_, ecr) in latest.items():
        skill = POSITIONS.get(position)
        if skill is None:
            continue
        numbered = {i: v for i, v in enumerate(ecr.values())}
        players = list(ecr)
        for i, rank in position_ranks(numbered).items():
            line = lines.get((players[i], season, week_number))
            if line is None:
                continue  # didn't play: graded the same way as our projections
            box = {stat: float(line[col] or 0) for stat, col in STAT_COLUMNS.items()}
            box["fumbles_lost"] = float(
                sum(line[c] or 0 for c in ("sack_fumbles_lost", "rushing_fumbles_lost",
                                            "receiving_fumbles_lost"))
            )  # fmt: skip
            box["two_pt_conversions"] = float(
                sum(line[c] or 0 for c in ("passing_2pt_conversions", "rushing_2pt_conversions",
                                            "receiving_2pt_conversions"))
            )  # fmt: skip
            for config in SCORING_CONFIGS:
                points = fantasy_points(config["weights"], skill, box)
                pairs.setdefault((season, position, config["key"]), []).append((rank, points))

    curves: dict[str, dict[str, dict[str, list[float]]]] = {}
    for season in graded:
        for config in SCORING_CONFIGS:
            for position, max_rank in MAX_RANK.items():
                history = [
                    pair
                    for s in range(args.first_season, season)
                    for pair in pairs.get((s, position, config["key"]), [])
                ]
                if history:
                    curves.setdefault(str(season), {}).setdefault(config["key"], {})[position] = [
                        round(v, 3) for v in fit_curve(history, max_rank)
                    ]
    args.out.write_text(
        json.dumps(
            {
                "generated": datetime.now(UTC).date().isoformat(),
                "source": "FantasyPros weekly ECR and nflverse player stats via nflreadpy "
                f"(CC-BY 4.0), regular seasons from {args.first_season}",
                "note": "curves[graded season] are fitted only on seasons before it",
                "curves": curves,
            },
            indent=1,
        )
        + "\n"
    )
    print(f"wrote {args.out.relative_to(BACKEND)}: graded seasons {sorted(curves)}")


def _kickoff(r: dict[str, object]) -> datetime:
    """Close enough to place a scrape date in its week: Eastern time, ignoring daylight saving."""
    moment = datetime.fromisoformat(f"{r['gameday']}T{r['gametime'] or '13:00'}:00-05:00")
    return moment.astimezone(UTC)


if __name__ == "__main__":
    main()
