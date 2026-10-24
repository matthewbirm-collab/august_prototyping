"""Capture live prop snapshots.

    uv run run-ingest --tier auto       # one run: poll every game that is due, then exit
    uv run run-ingest --tier 15m        # only games currently in the 15-minute tier
    uv run run-ingest --dry-run         # show what is due and the projected credit spend
    uv run run-ingest --serve           # local: repeat `--tier auto` on a timer (APScheduler)

Each game's new quotes also update `consensus_snapshots` (see `ingest/consensus_job.py`).

Production runs `--tier auto` (or one `--tier` per schedule) from a scheduled job; each run
exits when it is done. Needs ODDS_API_KEY, except for --dry-run.
"""

import argparse
import logging
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.logging import configure_logging
from app.settings import Settings, get_settings
from db.session import get_engine
from ingest import tiers
from ingest.capture import (
    CaptureConfig,
    OddsCapture,
    RunSummary,
    credits_used_today,
    has_previous_runs,
)
from ingest.consensus_job import ConsensusJob, JobConfig
from ingest.odds.factory import get_odds_provider
from ingest.odds.markets import ALTERNATE_MARKETS, MAIN_MARKETS
from ingest.odds.offline import OfflineProvider
from ingest.odds.provider import OddsProvider
from ingest.projection_job import ProjectionJob, ProjectionJobConfig
from ingest.recompute import RecomputeHook, chain, log_recompute

logger = logging.getLogger(__name__)

# Keeps two overlapping runs (e.g. a slow run and the next scheduled one) from double-spending.
RUN_LOCK_KEY = 7_262_001
SERVE_INTERVAL_MINUTES = 5


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="run-ingest", description=__doc__.splitlines()[0])
    parser.add_argument("--tier", default="auto", help="auto, or an interval like 15m, 1h, 3h")
    parser.add_argument("--dry-run", action="store_true", help="make no API calls")
    parser.add_argument("--serve", action="store_true", help="repeat on a timer until stopped")
    parser.add_argument("--serve-interval-minutes", type=int, default=SERVE_INTERVAL_MINUTES)
    parser.add_argument("--markets", help="comma-separated market keys (default: main markets)")
    parser.add_argument(
        "--include-alternates", action="store_true", help="also request alternate-line markets"
    )
    parser.add_argument("--bookmakers", help="comma-separated bookmaker keys (default: region)")
    args = parser.parse_args(argv)
    if args.tier != "auto":
        try:
            tiers.parse_interval(args.tier)
        except ValueError as exc:
            parser.error(str(exc))
    if args.serve and args.dry_run:
        parser.error("--serve and --dry-run can't be combined")
    if args.serve_interval_minutes < 1:
        parser.error("--serve-interval-minutes must be at least 1")
    args.markets = _split(args.markets)
    args.bookmakers = _split(args.bookmakers)
    return args


def _split(value: str | None) -> list[str] | None:
    items = [item.strip() for item in (value or "").split(",") if item.strip()]
    return items or None


def choose_markets(args: argparse.Namespace) -> list[str]:
    if args.markets:
        return list(args.markets)
    return [*MAIN_MARKETS, *(ALTERNATE_MARKETS if args.include_alternates else ())]


def projection_text(
    capture: OddsCapture, config: CaptureConfig, settings: Settings, now: datetime
) -> str:
    upcoming = capture.upcoming_games(now)
    projection = tiers.project_spend(
        [g.kickoff_utc for g in upcoming],
        now,
        config.tiers,
        config.credits_per_poll,
        settings.daily_credit_budget,
        config.horizon_days,
    )
    unlinked = sum(1 for g in upcoming if not g.odds_event_id)
    text_ = tiers.format_projection(projection, settings.daily_credit_budget)
    if unlinked:
        text_ += (
            f"\n{unlinked} of those games aren't linked to an odds event yet (a run links them)."
        )
    return text_


def dry_run_text(
    capture: OddsCapture, config: CaptureConfig, settings: Settings, tier: str, now: datetime
) -> str:
    due, unlinked = capture.due_games(now, tier)
    lines = [projection_text(capture, config, settings, now), ""]
    lines.append(
        f"Due now ({tier}): {len(due)} games, {len(due) * config.credits_per_poll} credits."
    )
    for game in due:
        hours = tiers.hours_to_kickoff(game.kickoff_utc, now)
        lines.append(f"  {game.id}  kickoff in {hours:.1f}h")
    if unlinked:
        lines.append(f"{unlinked} upcoming games have no odds event yet.")
    return "\n".join(lines)


def summary_text(summary: RunSummary) -> str:
    if summary.status == "idle":
        extra = (
            f" ({summary.unlinked_games} games not linked to an odds event)"
            if summary.unlinked_games
            else ""
        )
        return f"Nothing due.{extra}"
    if summary.status == "budget_exhausted":
        return f"Daily credit budget spent; {len(summary.deferred)} due games deferred."
    lines = [
        f"Run {summary.run_id}: {summary.status}. {summary.games_polled} games, "
        f"{summary.quotes} quotes, {summary.new_snapshots} new snapshots, "
        f"{summary.unmatched_names} unmatched names, {summary.credits_used} credits"
        + (
            f" ({summary.credits_remaining} left on the plan)."
            if summary.credits_remaining is not None
            else "."
        )
    ]
    lines += [f"  error: {e['game']}: {e['error']}" for e in summary.errors]
    if summary.deferred:
        lines.append(f"  deferred to the next run: {', '.join(summary.deferred)}")
    return "\n".join(lines)


@contextmanager
def run_lock(engine: Engine) -> Iterator[bool]:
    """Hold a Postgres advisory lock for the duration of a run; yields False if it's taken."""
    with engine.connect() as conn:
        got = bool(
            conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": RUN_LOCK_KEY}).scalar()
        )
        try:
            yield got
        finally:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": RUN_LOCK_KEY})
                conn.commit()


def run_once(
    session: Session,
    provider: OddsProvider,
    config: CaptureConfig,
    tier: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    on_new_snapshots: RecomputeHook = log_recompute,
) -> RunSummary:
    return OddsCapture(
        session, provider, config, clock=clock, on_new_snapshots=on_new_snapshots
    ).run(tier)


def build_scheduler(job: Callable[[], object], interval_minutes: int) -> BlockingScheduler:
    """A scheduler that runs `job` now and every `interval_minutes`, one run at a time."""
    scheduler = BlockingScheduler(timezone=UTC)
    scheduler.add_job(
        job,
        IntervalTrigger(minutes=interval_minutes, timezone=UTC),
        id="capture",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC),
    )
    return scheduler


def main(
    argv: Sequence[str] | None = None,
    *,
    session: Session | None = None,
    provider: OddsProvider | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    config = CaptureConfig.from_settings(settings, choose_markets(args), args.bookmakers)

    if args.dry_run:
        with _session(session) as db:
            capture = OddsCapture(db, provider or OfflineProvider(), config, clock=clock)
            print(dry_run_text(capture, config, settings, args.tier, clock()))
        return 0

    def job() -> int:
        return _run_job(settings, config, args.tier, session, provider, clock)

    if args.serve:
        with _session(session) as db:
            capture = OddsCapture(db, provider or OfflineProvider(), config, clock=clock)
            print(projection_text(capture, config, settings, clock()))
        print(f"Capturing every {args.serve_interval_minutes} minutes; Ctrl-C to stop.")
        scheduler = build_scheduler(job, args.serve_interval_minutes)
        with suppress(KeyboardInterrupt, SystemExit):
            scheduler.start()
        return 0
    return job()


def _run_job(
    settings: Settings,
    config: CaptureConfig,
    tier: str,
    session: Session | None,
    provider: OddsProvider | None,
    clock: Callable[[], datetime],
) -> int:
    """One run, with its own session, provider and run lock (unless injected by a test)."""
    engine = get_engine() if session is None else None
    try:
        with run_lock(engine) if engine is not None else _no_lock() as got:
            if not got:
                print("Another run is in progress; skipping.")
                return 0
            with _session(session) as db:
                if not has_previous_runs(db):
                    capture = OddsCapture(db, OfflineProvider(), config, clock=clock)
                    print(projection_text(capture, config, settings, clock()))
                    print()
                owns_provider = provider is None
                live = provider or get_odds_provider(
                    settings, used_today=credits_used_today(db, clock())
                )
                try:
                    hook = chain(
                        ConsensusJob(db, JobConfig.from_settings(settings)),
                        ProjectionJob(db, ProjectionJobConfig.from_settings(settings)),
                    )
                    summary = run_once(db, live, config, tier, clock, hook)
                finally:
                    if owns_provider:
                        close = getattr(live, "close", None)
                        if callable(close):
                            close()
                print(summary_text(summary))
                return 1 if summary.status in {"partial", "failed"} else 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


@contextmanager
def _no_lock() -> Iterator[bool]:
    yield True


@contextmanager
def _session(injected: Session | None) -> Iterator[Session]:
    if injected is not None:
        yield injected
        return
    with Session(get_engine()) as db:
        yield db


if __name__ == "__main__":
    sys.exit(main())
