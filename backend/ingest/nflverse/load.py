"""Load nflverse datasets into the database. Every load is an upsert, so reruns are safe."""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import polars as pl
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from db.base import Base
from db.models import (
    ActualStats,
    DepthChart,
    FfRanking,
    Game,
    IngestionRun,
    Injury,
    Player,
    Roster,
    SnapCount,
)
from ingest.nflverse import transform
from ingest.nflverse.source import NflverseSource

logger = logging.getLogger(__name__)

# Load order: games and players first, since everything else references them.
DATASETS = (
    "schedules",
    "players",
    "rosters",
    "injuries",
    "depth_charts",
    "snap_counts",
    "player_stats",
    "ff_rankings",
)
CHUNK = 1000


def upsert(
    session: Session,
    model: type[Base],
    rows: Sequence[dict[str, Any]],
    keys: Sequence[str],
    *,
    exclude: Sequence[str] = (),
) -> int:
    """Insert rows in chunks, updating every non-key column (except `exclude`) on conflict."""
    for start in range(0, len(rows), CHUNK):
        chunk = rows[start : start + CHUNK]
        stmt = insert(model).values(list(chunk))
        updates = {
            col: stmt.excluded[col] for col in chunk[0] if col not in keys and col not in exclude
        }
        if updates:
            session.execute(stmt.on_conflict_do_update(index_elements=list(keys), set_=updates))
        else:
            session.execute(stmt.on_conflict_do_nothing(index_elements=list(keys)))
    return len(rows)


class NflverseLoader:
    def __init__(self, session: Session, source: NflverseSource) -> None:
        self._session = session
        self._source = source

    def run(
        self, season: int, week: int | None = None, datasets: Sequence[str] = DATASETS
    ) -> dict[str, int]:
        """Load the datasets in dependency order and record an ingestion run. Commits."""
        unknown = set(datasets) - set(DATASETS)
        if unknown:
            raise ValueError(f"unknown datasets: {sorted(unknown)}")
        ordered = [d for d in DATASETS if d in datasets]

        run = IngestionRun(kind="nflverse", details={"season": season, "week": week})
        self._session.add(run)
        self._session.commit()

        counts: dict[str, int] = {}
        errors: list[dict[str, str]] = []
        for dataset in ordered:
            try:
                counts[dataset] = getattr(self, f"load_{dataset}")(season, week)
                self._session.commit()
            except Exception as exc:
                self._session.rollback()
                logger.exception("nflverse load failed", extra={"dataset": dataset})
                errors.append({"dataset": dataset, "error": f"{type(exc).__name__}: {exc}"})
            else:
                logger.info(
                    "nflverse dataset loaded",
                    extra={"dataset": dataset, "rows": counts[dataset], "season": season},
                )

        run.finished_at = datetime.now(UTC)
        run.status = "ok" if not errors else ("partial" if counts else "failed")
        run.errors = errors
        run.details = {"season": season, "week": week, "rows": counts}
        self._session.commit()
        return counts

    # Datasets ---------------------------------------------------------------------------------

    def load_schedules(self, season: int, week: int | None) -> int:
        # Always the whole season: kickoffs for every week anchor the other datasets' timing.
        rows = transform.schedule_rows(self._source.schedules([season]))
        return upsert(self._session, Game, rows, ["id"], exclude=["odds_event_id"])

    def load_players(self, season: int, week: int | None) -> int:
        rows = transform.player_rows(
            self._source.rosters_weekly([season]), self._source.ff_playerids()
        )
        for row in rows:
            row["updated_at"] = datetime.now(UTC)
        # Loading an older season (a backfill) must not move a player back to an old team.
        # Schedules load first, so the newest season in `games` is the newest one we know about.
        newest = self._session.scalar(select(func.max(Game.season)))
        stale = newest is not None and season < newest
        return upsert(
            self._session,
            Player,
            rows,
            ["gsis_id"],
            exclude=["team_abbr", "position", "updated_at"] if stale else (),
        )

    def load_rosters(self, season: int, week: int | None) -> int:
        rosters = _week(self._source.rosters_weekly([season]), week)
        rows = transform.roster_rows(rosters, self._players_by("gsis"), self._kickoffs())
        return upsert(self._session, Roster, rows, ["season", "week", "player_id"])

    def load_injuries(self, season: int, week: int | None) -> int:
        injuries = _week(self._source.injuries([season]), week)
        rows = transform.injury_rows(injuries, self._players_by("gsis"), self._kickoffs())
        return upsert(self._session, Injury, rows, ["season", "week", "player_id", "available_at"])

    def load_depth_charts(self, season: int, week: int | None) -> int:
        rows = transform.depth_chart_rows(
            self._source.depth_charts([season]), self._players_by("gsis"), self._kickoffs()
        )
        rows = [r for r in rows if r["season"] == season and (week is None or r["week"] == week)]
        return upsert(
            self._session,
            DepthChart,
            rows,
            ["season", "week", "player_id", "position", "available_at"],
        )

    def load_snap_counts(self, season: int, week: int | None) -> int:
        snaps = _week(self._source.snap_counts([season]), week)
        rows = transform.snap_count_rows(snaps, self._players_by("pfr"), self._game_ids(season))
        return upsert(self._session, SnapCount, rows, ["game_id", "player_id"])

    def load_player_stats(self, season: int, week: int | None) -> int:
        stats = _week(self._source.player_stats([season]), week)
        rows = transform.actual_stat_rows(stats, self._players_by("gsis"), self._game_ids(season))
        return upsert(self._session, ActualStats, rows, ["game_id", "player_id"])

    def load_ff_rankings(self, season: int, week: int | None) -> int:
        rows = transform.ranking_rows(
            self._source.ff_rankings(), self._players_by("fantasypros"), self._kickoffs(), {season}
        )
        rows = [r for r in rows if week is None or r["week"] == week]
        return upsert(
            self._session, FfRanking, rows, ["scrape_date", "page_type", "fantasypros_id"]
        )

    # Lookups ----------------------------------------------------------------------------------

    def _players_by(self, id_kind: str) -> dict[str, int]:
        if id_kind == "gsis":
            stmt = select(Player.gsis_id, Player.id).where(Player.gsis_id.is_not(None))
        else:
            column = Player.external_ids[id_kind].astext
            stmt = select(column, Player.id).where(column.is_not(None))
        return {key: player_id for key, player_id in self._session.execute(stmt) if key}

    def _kickoffs(self) -> transform.KickoffIndex:
        rows = self._session.execute(
            select(Game.season, Game.week, Game.home_abbr, Game.away_abbr, Game.kickoff_utc)
        )
        return transform.KickoffIndex.build(
            (r.season, r.week, r.home_abbr, r.away_abbr, r.kickoff_utc) for r in rows
        )

    def _game_ids(self, season: int) -> set[str]:
        return set(self._session.scalars(select(Game.id).where(Game.season == season)))


def _week(frame: pl.DataFrame, week: int | None) -> pl.DataFrame:
    return frame if week is None else frame.filter(pl.col("week") == week)
