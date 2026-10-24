"""Where nflverse data comes from: nflreadpy in production, recorded Parquet files in tests."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import nflreadpy as nfl
import polars as pl
from nflreadpy.config import update_config

IN_SEASON_CACHE_SECONDS = 6 * 3600
OFF_SEASON_CACHE_SECONDS = 7 * 24 * 3600
IN_SEASON_MONTHS = frozenset({9, 10, 11, 12, 1, 2})


class NflverseSource(Protocol):
    def schedules(self, seasons: Sequence[int]) -> pl.DataFrame: ...
    def rosters_weekly(self, seasons: Sequence[int]) -> pl.DataFrame: ...
    def injuries(self, seasons: Sequence[int]) -> pl.DataFrame: ...
    def depth_charts(self, seasons: Sequence[int]) -> pl.DataFrame: ...
    def snap_counts(self, seasons: Sequence[int]) -> pl.DataFrame: ...
    def player_stats(self, seasons: Sequence[int]) -> pl.DataFrame: ...
    def ff_playerids(self) -> pl.DataFrame: ...
    def ff_rankings(self) -> pl.DataFrame:
        """The full FantasyPros archive (`load_ff_rankings(type="all")`)."""
        ...


def cache_seconds(now: datetime) -> int:
    """Six hours during the season (September to February), a week otherwise."""
    return IN_SEASON_CACHE_SECONDS if now.month in IN_SEASON_MONTHS else OFF_SEASON_CACHE_SECONDS


class NflreadpySource:
    """Live nflverse data with nflreadpy's filesystem cache."""

    def __init__(
        self, cache_dir: Path, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        update_config(
            cache_mode="filesystem",
            cache_dir=cache_dir,
            cache_duration=cache_seconds(clock()),
        )

    def schedules(self, seasons: Sequence[int]) -> pl.DataFrame:
        return cast(pl.DataFrame, nfl.load_schedules(list(seasons)))

    def rosters_weekly(self, seasons: Sequence[int]) -> pl.DataFrame:
        return cast(pl.DataFrame, nfl.load_rosters_weekly(list(seasons)))

    def injuries(self, seasons: Sequence[int]) -> pl.DataFrame:
        return cast(pl.DataFrame, nfl.load_injuries(list(seasons)))

    def depth_charts(self, seasons: Sequence[int]) -> pl.DataFrame:
        # The source changed after 2024, so seasons on either side have different columns.
        frames = [cast(pl.DataFrame, nfl.load_depth_charts([season])) for season in seasons]
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def snap_counts(self, seasons: Sequence[int]) -> pl.DataFrame:
        return cast(pl.DataFrame, nfl.load_snap_counts(list(seasons)))

    def player_stats(self, seasons: Sequence[int]) -> pl.DataFrame:
        return cast(pl.DataFrame, nfl.load_player_stats(list(seasons)))

    def ff_playerids(self) -> pl.DataFrame:
        return cast(pl.DataFrame, nfl.load_ff_playerids())

    def ff_rankings(self) -> pl.DataFrame:
        return cast(pl.DataFrame, nfl.load_ff_rankings(type="all"))


class ParquetSource:
    """Recorded samples (see scripts/record_nflverse_fixtures.py), filtered like the real thing."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _read(self, name: str) -> pl.DataFrame:
        return pl.read_parquet(self._dir / f"{name}.parquet")

    def _seasons(self, name: str, seasons: Sequence[int]) -> pl.DataFrame:
        return self._read(name).filter(pl.col("season").is_in(list(seasons)))

    def schedules(self, seasons: Sequence[int]) -> pl.DataFrame:
        return self._seasons("schedules", seasons)

    def rosters_weekly(self, seasons: Sequence[int]) -> pl.DataFrame:
        return self._seasons("rosters_weekly", seasons)

    def injuries(self, seasons: Sequence[int]) -> pl.DataFrame:
        return self._seasons("injuries", seasons)

    def depth_charts(self, seasons: Sequence[int]) -> pl.DataFrame:
        frames = [
            self._read(f"depth_charts_{season}")
            for season in seasons
            if (self._dir / f"depth_charts_{season}.parquet").exists()
        ]
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def snap_counts(self, seasons: Sequence[int]) -> pl.DataFrame:
        return self._seasons("snap_counts", seasons)

    def player_stats(self, seasons: Sequence[int]) -> pl.DataFrame:
        return self._seasons("player_stats", seasons)

    def ff_playerids(self) -> pl.DataFrame:
        return self._read("ff_playerids")

    def ff_rankings(self) -> pl.DataFrame:
        return self._read("ff_rankings")
