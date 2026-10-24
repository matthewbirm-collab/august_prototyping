"""Replay past weeks: project every game as of each snapshot time and grade it.

    uv run backtest --seasons 2023,2024,2025 --snapshots t24h,t1h,close
    uv run backtest --seasons 2023,2024,2025 --snapshots close --model-only

For each finished regular-season game and snapshot label, the moment is the backfill's recorded
snapshot time (or kickoff minus 24h / 1h / 1 minute when there is none). The consensus is
recomputed as of that moment from the stored quotes, projections are computed in memory (never
written to `projections`) using only data dated no later than it, and graded against
`actual_stats`. `--model-only` ignores the markets and projects everything from the gap-fill
model, which needs no historical odds.

Writes `summary.md`, `by_position.csv`, `by_week.csv` and `coverage.png` to `reports/backtest/`,
and exits 1 when any position's MAE is more than 5% worse than the saved baseline
(`config/backtest_baseline.json`; `--update-baseline` rewrites it).
"""

import argparse
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from analytics.backtest import (
    Graded,
    baseline_from,
    evaluate,
    load_baseline,
    regressions,
    save_baseline,
    write_reports,
)
from analytics.ecr import EcrCurves, load_curves, position_ranks
from app.logging import configure_logging
from app.settings import get_settings
from db.models import ActualStats, BackfillProgress, FfRanking, Game, ScoringConfig
from db.session import get_engine
from engine.projection import FantasyProjection
from engine.scoring import BOX_SCORE_WEIGHTS, Position, ScoringWeights, fantasy_points
from ingest.backfill_plan import HOURS_OUT
from ingest.consensus_job import ConsensusJob, JobConfig
from ingest.projection_job import ProjectionJob, ProjectionJobConfig

BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = BACKEND / "config" / "backtest_baseline.json"
CLOSE_OFFSET = timedelta(minutes=1)
LABELS = (*HOURS_OUT, "close")


def default_moment(kickoff: datetime, label: str) -> datetime:
    if label == "close":
        return kickoff - CLOSE_OFFSET
    return kickoff - timedelta(hours=HOURS_OUT[label])


class Backtest:
    def __init__(
        self,
        session: Session,
        scoring: str,
        *,
        model_only: bool = False,
        curves: EcrCurves | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session = session
        self._scoring = scoring
        self._model_only = model_only
        self._curves = curves if curves is not None else load_curves()
        settings = get_settings()
        config = ProjectionJobConfig.from_settings(settings)
        self._projections = ProjectionJob(session, replace(config, use_markets=not model_only))
        self._consensus = (
            None if model_only else ConsensusJob(session, JobConfig.from_settings(settings))
        )
        row = session.scalars(select(ScoringConfig).where(ScoringConfig.key == scoring)).first()
        if row is None:
            raise ValueError(f"no scoring config {scoring!r}")
        self._weights = ScoringWeights.model_validate(row.weights)
        self._clock = clock
        self.did_not_play = 0
        self.skipped = 0

    def games(self, seasons: Sequence[int], weeks: Sequence[int] | None = None) -> list[Game]:
        stmt = (
            select(Game)
            .where(
                Game.season.in_(seasons),
                Game.season_type == "REG",
                Game.kickoff_utc < self._clock(),
            )
            .order_by(Game.kickoff_utc, Game.id)
        )
        if weeks:
            stmt = stmt.where(Game.week.in_(weeks))
        return list(self._session.scalars(stmt))

    def moment(self, game: Game, label: str) -> datetime:
        recorded = self._session.scalar(
            select(BackfillProgress.snapshot_at).where(
                BackfillProgress.game_id == game.id, BackfillProgress.label == label
            )
        )
        return recorded or default_moment(game.kickoff_utc, label)

    def run(
        self, seasons: Sequence[int], labels: Sequence[str], weeks: Sequence[int] | None = None
    ) -> list[Graded]:
        graded: list[Graded] = []
        for game in self.games(seasons, weeks):
            actuals = {
                a.player_id: a
                for a in self._session.scalars(
                    select(ActualStats).where(ActualStats.game_id == game.id)
                )
            }
            for label in labels:
                graded.extend(self.grade_game(game, label, actuals))
        return graded

    def project(self, game: Game, as_of: datetime) -> list[tuple[int, Position, FantasyProjection]]:
        """(player, position, projection) for the game as of `as_of`: what the live app would
        have shown then."""
        if self._consensus is not None:
            self._consensus.recompute(game.id, None, as_of)
        found, skipped = self._projections.compute(game.id, None, as_of, [self._scoring])
        self.skipped += skipped
        return [(c.player_id, c.position, c.projection) for c in found]

    def grade_game(self, game: Game, label: str, actuals: dict[int, ActualStats]) -> list[Graded]:
        as_of = self.moment(game, label)
        ecr = self._ecr(game, as_of)
        rows = []
        for player_id, position, projection in self.project(game, as_of):
            actual = actuals.get(player_id)
            if actual is None:
                self.did_not_play += 1
                continue
            box = {stat: float(getattr(actual, stat)) for stat in BOX_SCORE_WEIGHTS}
            rows.append(
                Graded(
                    season=game.season,
                    week=game.week,
                    game_id=game.id,
                    label=label,
                    player_id=player_id,
                    position=position,
                    projected=projection.mean,
                    p20=projection.p20,
                    p80=projection.p80,
                    actual=fantasy_points(self._weights, position, box),
                    ecr=ecr.get(player_id),
                    model_share=_model_share(projection),
                    confidence=projection.confidence.score,
                )
            )
        return rows

    def _ecr(self, game: Game, as_of: datetime) -> dict[int, float]:
        """player -> ECR baseline points, from each position's latest ranking scraped on a day
        before `as_of` (a scrape dated the game day itself might be from after kickoff)."""
        latest: dict[str, date] = {}
        rows = list(
            self._session.scalars(
                select(FfRanking).where(
                    FfRanking.season == game.season,
                    FfRanking.week == game.week,
                    FfRanking.scrape_date < as_of.date(),
                )
            )
        )
        for row in rows:
            latest[row.page_type] = max(row.scrape_date, latest.get(row.page_type, row.scrape_date))
        by_page: dict[str, dict[int, float]] = defaultdict(dict)
        positions: dict[str, str] = {}
        for row in rows:
            if row.scrape_date == latest[row.page_type]:
                by_page[row.page_type][row.fantasypros_id] = row.ecr
                positions[row.page_type] = row.position
        ids = {(r.page_type, r.fantasypros_id): r.player_id for r in rows}
        points: dict[int, float] = {}
        for page, ecr in by_page.items():
            for fantasypros_id, rank in position_ranks(ecr).items():
                player_id = ids.get((page, fantasypros_id))
                value = self._curves.points(game.season, self._scoring, positions[page], rank)
                if player_id is not None and value is not None:
                    points[player_id] = value
        return points


def _model_share(projection: FantasyProjection) -> float:
    """Share of the stat points (fumbles aside) that came from gap-filled stats."""
    total = sum(abs(b.points) for b in projection.breakdown.values() if b.source != "history")
    modelled = sum(abs(b.points) for b in projection.breakdown.values() if b.source == "model")
    return modelled / total if total else 0.0


def _ints(text: str, name: str, parser: argparse.ArgumentParser) -> list[int]:
    try:
        return sorted({int(s) for s in text.split(",") if s.strip()})
    except ValueError:
        parser.error(f"{name} takes whole numbers, like 2023,2024")


def main(
    argv: Sequence[str] | None = None,
    *,
    session: Session | None = None,
    curves: EcrCurves | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = argparse.ArgumentParser(prog="backtest", description=__doc__.splitlines()[0])
    parser.add_argument("--seasons", required=True, help="comma-separated, e.g. 2023,2024,2025")
    parser.add_argument("--weeks", help="comma-separated weeks (default: all)")
    parser.add_argument("--snapshots", default="t24h,t1h,close", help=f"any of {', '.join(LABELS)}")
    parser.add_argument("--scoring", default="ppr", help="a scoring_configs key (default: ppr)")
    parser.add_argument("--model-only", action="store_true", help="ignore markets; gap-fill only")
    parser.add_argument("--out", type=Path, default=Path("reports") / "backtest")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args(argv)
    seasons = _ints(args.seasons, "--seasons", parser)
    weeks = _ints(args.weeks, "--weeks", parser) if args.weeks else None
    labels = [s.strip() for s in args.snapshots.split(",") if s.strip()]
    unknown = set(labels) - set(LABELS)
    if not seasons or not labels or unknown:
        parser.error(f"--seasons can't be empty and --snapshots takes {', '.join(LABELS)}")

    configure_logging(get_settings().log_level)
    owns = session is None
    db = session if session is not None else Session(get_engine())
    try:
        backtest = Backtest(
            db, args.scoring, model_only=args.model_only, curves=curves, clock=clock
        )
        rows = backtest.run(seasons, labels, weeks)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if owns:
            db.close()
    if not rows:
        print(
            f"Nothing to grade for seasons {args.seasons}: load nflverse data "
            "(`ingest-nflverse`) and, for market projections, run the historical backfill.",
            file=sys.stderr,
        )
        return 2

    kind = f"{'model' if args.model_only else 'market'}/{args.scoring}"
    reports = evaluate(rows)
    notes = [
        f"{'Gap-fill model only (no markets)' if args.model_only else 'Market projections'}, "
        f"{args.scoring} scoring, seasons {args.seasons}, snapshots {', '.join(labels)}.",
        f"{len(rows)} graded projections; {backtest.did_not_play} more for players with no stat "
        f"line (didn't play) and {backtest.skipped} players with nothing to project.",
        "ECR baseline: FantasyPros weekly ranks turned into points with a curve fitted on earlier "
        "seasons only, compared on the players both project.",
        "Game spreads and totals are nflverse's latest numbers, so earlier snapshots see "
        "the closing line.",
    ]
    gate_label = "close" if "close" in labels else labels[-1]
    written = write_reports(
        rows, reports, args.out, title=f"Backtest: {kind}", notes=notes, gate_label=gate_label
    )
    for report in reports.values():
        beats = {True: "beats ECR", False: "ECR better", None: "no ECR"}[report.beats_ecr]
        print(
            f"{report.label:<6}{report.position:<4}n={report.n:<6} MAE {report.mae:6.2f}  "
            f"Spearman {report.spearman or 0:5.2f}  below p20 {report.below_p20:5.1%}  "
            f"below p80 {report.below_p80:5.1%}  {beats}"
        )
    print(f"\nWrote {len(written)} files to {args.out}/ (see summary.md)")

    current = baseline_from(reports)
    if args.update_baseline:
        save_baseline(args.baseline, kind, current)
        print(f"Saved the baseline for {kind} to {args.baseline}")
        return 0
    worse = regressions(reports, load_baseline(args.baseline, kind))
    if worse:
        print("MAE regressed more than 5% from the baseline:", *worse, sep="\n  ", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
