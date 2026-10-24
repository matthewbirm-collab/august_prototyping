"""Check the closing consensus against what happened.

    uv run calibration-report --seasons 2023,2024,2025 --recompute

For every backfilled game with a closing snapshot, this takes the consensus as of that snapshot
(`--recompute` writes it from the stored quotes first), joins the player's real stat line, and
writes the per-market reliability tables and plots to `reports/calibration/`. Needs the historical
backfill; without it there is nothing to compare.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from analytics.calibration import (
    BUCKETS,
    MarketReport,
    Observation,
    evaluate,
    write_reports,
)
from app.distribution_params import DEFAULT_PATH, DistributionParams, load_distribution_params
from app.logging import configure_logging
from app.settings import get_settings
from db.models import (
    ActualStats,
    BackfillProgress,
    ConsensusSnapshot,
    Game,
    Market,
    Player,
)
from db.session import get_engine
from ingest.consensus_job import ConsensusJob, JobConfig

# The stat line behind each market, by `markets.stat`. Anytime TD counts rushing and receiving
# touchdowns, which is what the books settle on.
ACTUALS: dict[str, Callable[[ActualStats], float]] = {
    "pass_yds": lambda a: a.pass_yds,
    "pass_tds": lambda a: a.pass_tds,
    "pass_completions": lambda a: a.completions,
    "interceptions": lambda a: a.interceptions,
    "rush_yds": lambda a: a.rush_yds,
    "rush_attempts": lambda a: a.rush_attempts,
    "receptions": lambda a: a.receptions,
    "rec_yds": lambda a: a.rec_yds,
    "anytime_td": lambda a: a.rush_tds + a.rec_tds,
}


@dataclass
class Collected:
    observations: list[Observation]
    games: int = 0
    without_stats: int = 0
    """Consensus rows for players with no stat line (inactive: the prop is void)."""


def closing_snapshots(session: Session, seasons: Sequence[int], label: str) -> dict[str, datetime]:
    """game id -> the time of its finished snapshot with this label."""
    rows = session.execute(
        select(BackfillProgress.game_id, BackfillProgress.snapshot_at)
        .join(Game, Game.id == BackfillProgress.game_id)
        .where(
            Game.season.in_(seasons),
            BackfillProgress.label == label,
            BackfillProgress.snapshot_at.is_not(None),
        )
    )
    return {game_id: snapshot_at for game_id, snapshot_at in rows if snapshot_at is not None}


def recompute(session: Session, closings: dict[str, datetime]) -> int:
    """Write the consensus as of each game's closing snapshot. Returns rows written."""
    job = ConsensusJob(session, JobConfig.from_settings(get_settings()))
    return sum(
        job.recompute(game_id, None, snapshot_at).written
        for game_id, snapshot_at in closings.items()
    )


def collect(
    session: Session, seasons: Sequence[int], label: str, params: DistributionParams
) -> Collected:
    closings = closing_snapshots(session, seasons, label)
    collected = Collected([], games=len(closings))
    if not closings:
        return collected

    columns = (ConsensusSnapshot.game_id, ConsensusSnapshot.player_id, ConsensusSnapshot.market_key)
    stmt = (
        select(ConsensusSnapshot)
        .join(
            BackfillProgress,
            (BackfillProgress.game_id == ConsensusSnapshot.game_id)
            & (BackfillProgress.label == label),
        )
        .where(
            ConsensusSnapshot.game_id.in_(list(closings)),
            ConsensusSnapshot.captured_at <= BackfillProgress.snapshot_at,
        )
        .ext(distinct_on(*columns))
        .order_by(*columns, ConsensusSnapshot.captured_at.desc())
    )
    consensus_rows = list(session.scalars(stmt))
    actuals = {
        (a.game_id, a.player_id): a
        for a in session.scalars(select(ActualStats).where(ActualStats.game_id.in_(list(closings))))
    }
    stats = dict(session.execute(select(Market.key, Market.stat)).all())
    positions = dict(session.execute(select(Player.id, Player.position)).all())

    for row in consensus_rows:
        stat = stats.get(row.market_key)
        actual = actuals.get((row.game_id, row.player_id))
        if stat not in ACTUALS:
            continue
        if actual is None:
            collected.without_stats += 1
            continue
        collected.observations.append(
            Observation(
                market=row.market_key,
                p_over=row.fair_p_over,
                line=row.fair_line,
                actual=float(ACTUALS[stat](actual)),
                model=params.model(stat, positions.get(row.player_id)),
            )
        )
    return collected


def summary_text(reports: dict[str, MarketReport]) -> str:
    lines = [
        f"{'market':<28}{'n':>6}{'went over':>11}{'predicted':>11}"
        f"{'Brier':>8}{'mean gap':>10}  range check"
    ]
    for r in reports.values():
        check = r.range_check.verdict if r.range_check else "-"
        lines.append(
            f"{r.market:<28}{r.n:>6}{r.base_rate:>11.1%}{r.mean_predicted:>11.1%}"
            f"{r.brier:>8.3f}{r.calibration_error:>10.3f}  {check}"
        )
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    session: Session | None = None,
    params: DistributionParams | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="calibration-report", description=__doc__.splitlines()[0])
    parser.add_argument("--seasons", required=True, help="comma-separated, e.g. 2023,2024,2025")
    parser.add_argument("--label", default="close", help="which snapshot (default: close)")
    parser.add_argument("--out", type=Path, default=Path("reports") / "calibration")
    parser.add_argument("--buckets", type=int, default=BUCKETS)
    parser.add_argument("--recompute", action="store_true", help="write the consensus first")
    args = parser.parse_args(argv)
    try:
        seasons = sorted({int(s) for s in args.seasons.split(",") if s.strip()})
    except ValueError:
        parser.error("--seasons takes whole numbers, like 2023,2024")
    if not seasons or args.buckets < 2:
        parser.error("--seasons can't be empty and --buckets must be at least 2")

    configure_logging(get_settings().log_level)
    shapes = params or load_distribution_params(
        get_settings().distribution_params_path or DEFAULT_PATH
    )
    owns = session is None
    db = session if session is not None else Session(get_engine())
    try:
        if args.recompute:
            closings = closing_snapshots(db, seasons, args.label)
            rows_written = recompute(db, closings)
            print(f"Consensus written for {rows_written} player markets in {len(closings)} games.")
        collected = collect(db, seasons, args.label, shapes)
        if not collected.observations:
            print(
                f"No closing consensus found for seasons {args.seasons}: run the historical "
                "backfill (`backfill-props`), then rerun with --recompute.",
                file=sys.stderr,
            )
            return 1
        reports = evaluate(collected.observations, args.buckets)
        written = write_reports(reports, args.out)
    finally:
        if owns:
            db.close()
    print(
        f"{len(collected.observations)} props from {collected.games} games"
        f" ({collected.without_stats} skipped: player has no stat line)\n"
    )
    print(summary_text(reports))
    print(f"\nWrote {len(written)} files to {args.out}/ (see summary.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
