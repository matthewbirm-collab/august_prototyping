"""Record small nflverse samples for tests into tests/fixtures/nflverse/.

    cd backend && uv run python scripts/record_nflverse_fixtures.py

Downloads public nflverse data (CC-BY 4.0) through nflreadpy and keeps only Arizona and Houston
(Marvin Harrison Jr. moved ARI -> HOU in 2026), skill positions, and the weeks the tests need.
"""

from pathlib import Path

import nflreadpy as nfl
import polars as pl
from nflreadpy.config import update_config

BACKEND = Path(__file__).resolve().parents[1]
OUT = BACKEND / "tests" / "fixtures" / "nflverse"
TEAMS = ["ARI", "HOU"]
SKILL = ["QB", "RB", "WR", "TE", "FB"]
SEASON, WEEKS = 2026, list(range(1, 7))
OLD_SEASON, OLD_WEEKS = 2024, [1, 2]


def save(name: str, df: pl.DataFrame) -> None:
    path = OUT / f"{name}.parquet"
    df.write_parquet(path, compression="zstd")
    print(f"{name}: {df.height} rows, {path.stat().st_size // 1024} KB")


def main() -> None:
    update_config(cache_mode="filesystem", cache_dir=BACKEND / ".cache" / "nflverse")
    OUT.mkdir(parents=True, exist_ok=True)

    schedules = nfl.load_schedules([OLD_SEASON, SEASON])
    save("schedules", schedules)

    rosters = nfl.load_rosters_weekly([SEASON]).filter(
        pl.col("team").is_in(TEAMS),
        pl.col("week").is_in(WEEKS),
        pl.col("position").is_in(SKILL),
    )
    save("rosters_weekly", rosters)
    gsis = set(rosters["gsis_id"].drop_nulls())

    save(
        "injuries",
        pl.concat(
            [
                nfl.load_injuries([OLD_SEASON]).filter(
                    pl.col("team").is_in(TEAMS), pl.col("week").is_in(OLD_WEEKS)
                ),
                nfl.load_injuries([SEASON]).filter(
                    pl.col("team").is_in(TEAMS), pl.col("week").is_in(WEEKS)
                ),
            ],
            how="diagonal_relaxed",
        ),
    )

    depth_new = nfl.load_depth_charts([SEASON]).filter(
        pl.col("team").is_in(TEAMS), pl.col("pos_abb").is_in(SKILL)
    )
    save("depth_charts_2026", depth_new)
    depth_old = nfl.load_depth_charts([OLD_SEASON]).filter(
        pl.col("club_code").is_in(TEAMS),
        pl.col("week").is_in(OLD_WEEKS),
        pl.col("formation") == "Offense",
    )
    save("depth_charts_2024", depth_old)

    save(
        "snap_counts",
        nfl.load_snap_counts([SEASON]).filter(
            pl.col("team").is_in(TEAMS),
            pl.col("week").is_in(WEEKS),
            pl.col("position").is_in(SKILL),
        ),
    )
    save(
        "player_stats",
        nfl.load_player_stats([SEASON]).filter(
            pl.col("team").is_in(TEAMS),
            pl.col("week").is_in(WEEKS),
            pl.col("position").is_in(SKILL),
        ),
    )

    ids = nfl.load_ff_playerids().filter(pl.col("gsis_id").is_in(gsis))
    save("ff_playerids", ids)

    rankings = nfl.load_ff_rankings(type="all")
    save(
        "ff_rankings",
        rankings.filter(
            pl.col("ecr_type") == "wp",
            pl.col("scrape_date").str.starts_with(str(SEASON)),
            pl.col("id").is_in({str(i) for i in ids["fantasypros_id"].drop_nulls()}),
        ),
    )


if __name__ == "__main__":
    main()
