"""Backfill historical prop snapshots for past regular seasons (paid Odds API plans only).

    uv run backfill-props --seasons 2023,2024,2025               # estimate only; spends nothing
    uv run backfill-props --seasons 2025 --weeks 8 --confirm     # a pilot week
    uv run backfill-props --seasons 2023,2024,2025 --confirm     # everything not yet captured
    uv run backfill-props --seasons 2025 --probe --confirm       # 1 credit: is history available?
    uv run backfill-props --seasons 2023,2024,2025 --report      # status and unmatched rates

Nothing is spent without --confirm. Start with a pilot week, then review the names the resolver
couldn't match (`python -m ingest.resolver_cli list`) and confirm their aliases before the full
run: quotes for a name that can't be matched are not kept, and finished snapshots are not bought
again. Runs resume where they stopped.
"""

import argparse
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.logging import configure_logging
from app.settings import Settings, get_settings
from db.session import get_engine
from ingest import backfill_plan as plan
from ingest.backfill import (
    Backfill,
    BackfillConfig,
    BackfillSummary,
    LinkReport,
    StopRunError,
    format_report,
)
from ingest.nflverse.load import NflverseLoader
from ingest.nflverse.source import NflreadpySource, NflverseSource
from ingest.odds.factory import get_odds_provider
from ingest.odds.markets import ALTERNATE_MARKETS, MAIN_MARKETS
from ingest.odds.offline import OfflineProvider
from ingest.odds.provider import OddsProvider, OddsProviderError

# A different lock from live capture, so a long backfill doesn't block live polling.
BACKFILL_LOCK_KEY = 7_262_002
PREREQUISITE_DATASETS = ["schedules", "players", "rosters", "player_stats"]
CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache" / "nflverse"


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backfill-props", description=__doc__.splitlines()[0], epilog=__doc__
    )
    parser.add_argument("--seasons", required=True, help="comma-separated, e.g. 2023,2024,2025")
    parser.add_argument("--weeks", help="e.g. 8, 1-4 or 1,3,5 (default: all)")
    parser.add_argument(
        "--snapshots",
        default=",".join(plan.LABELS),
        help=f"any of {', '.join(plan.LABELS)} (default: all)",
    )
    parser.add_argument("--markets", help="comma-separated market keys (default: main markets)")
    parser.add_argument("--include-alternates", action="store_true")
    parser.add_argument("--bookmakers", help="comma-separated bookmaker keys (default: region)")
    parser.add_argument("--confirm", action="store_true", help="actually spend credits")
    parser.add_argument("--max-credits", type=int, help="cap for this run (default: the estimate)")
    parser.add_argument(
        "--reserve", type=int, default=1000, help="credits to leave on the plan (default: 1000)"
    )
    parser.add_argument("--max-rps", type=float, default=2.0, help="requests per second")
    parser.add_argument("--probe", action="store_true", help="1-credit check that history works")
    parser.add_argument("--report", action="store_true", help="show status and exit")
    parser.add_argument("--skip-nflverse", action="store_true", help="skip the free data loads")
    args = parser.parse_args(argv)

    args.seasons = _ints(parser, args.seasons, "--seasons")
    args.weeks = _weeks(parser, args.weeks)
    args.snapshots = [s.strip() for s in args.snapshots.split(",") if s.strip()]
    bad = set(args.snapshots) - set(plan.LABELS)
    if bad or not args.snapshots:
        parser.error(f"unknown snapshots: {', '.join(sorted(bad)) or '(none)'}")
    args.snapshots = [label for label in plan.LABELS if label in args.snapshots]
    args.markets = _names(args.markets)
    args.bookmakers = _names(args.bookmakers)
    if args.max_rps <= 0:
        parser.error("--max-rps must be positive")
    if (args.max_credits or 0) < 0 or args.reserve < 0:
        parser.error("--max-credits and --reserve can't be negative")
    return args


def _ints(parser: argparse.ArgumentParser, value: str, flag: str) -> list[int]:
    try:
        numbers = sorted({int(item) for item in value.split(",") if item.strip()})
    except ValueError:
        parser.error(f"{flag} takes whole numbers, like 2023,2024")
    if not numbers:
        parser.error(f"{flag} is empty")
    return numbers


def _weeks(parser: argparse.ArgumentParser, value: str | None) -> list[int] | None:
    if not value:
        return None
    weeks: set[int] = set()
    try:
        for part in value.split(","):
            low, _, high = part.strip().partition("-")
            weeks.update(range(int(low), int(high or low) + 1))
    except ValueError:
        parser.error("--weeks takes numbers and ranges, like 8 or 1-4 or 1,3,5")
    return sorted(weeks)


def _names(value: str | None) -> list[str] | None:
    items = [item.strip() for item in (value or "").split(",") if item.strip()]
    return items or None


def choose_markets(args: argparse.Namespace) -> list[str]:
    if args.markets:
        return list(args.markets)
    return [*MAIN_MARKETS, *(ALTERNATE_MARKETS if args.include_alternates else ())]


def build_config(settings: Settings, args: argparse.Namespace) -> BackfillConfig:
    return BackfillConfig(
        source=settings.odds_provider,
        markets=tuple(choose_markets(args)),
        regions=settings.odds_api_regions,
        fuzzy_threshold=settings.fuzzy_match_threshold,
        stale_after=timedelta(minutes=settings.stale_quote_minutes),
        keepalive=timedelta(minutes=settings.quote_keepalive_minutes),
        labels=tuple(args.snapshots),
        bookmakers=tuple(args.bookmakers) if args.bookmakers else None,
        max_rps=args.max_rps,
        reserve=args.reserve,
    )


@contextmanager
def backfill_lock(engine: Engine) -> Iterator[bool]:
    with engine.connect() as conn:
        got = bool(
            conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": BACKFILL_LOCK_KEY}).scalar()
        )
        try:
            yield got
        finally:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": BACKFILL_LOCK_KEY})
                conn.commit()


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


def load_prerequisites(session: Session, source: NflverseSource, seasons: Sequence[int]) -> None:
    """Free nflverse loads the backfill needs: games to poll, rosters to resolve names against
    and box scores to grade with. Newest season first, so current teams stay current."""
    loader = NflverseLoader(session, source)
    for season in sorted(seasons, reverse=True):
        counts = loader.run(season, datasets=PREREQUISITE_DATASETS)
        loaded = ", ".join(f"{name} {n:,}" for name, n in counts.items())
        print(f"nflverse {season}: {loaded or 'nothing loaded'}")


def summary_text(summary: BackfillSummary, link: LinkReport | None) -> str:
    lines = []
    if link is not None:
        lines.append(
            f"Linked {link.linked} games to historical events in {link.weeks} weekly calls "
            f"({link.credits} credits); {len(link.unlinked)} still unlinked."
        )
    if summary.status == "idle":
        lines.append("Nothing left to capture for these games.")
        return "\n".join(lines)
    shown = ", ".join(f"{n} {status}" for status, n in sorted(summary.snapshots.items()))
    lines.append(
        f"Run {summary.run_id}: {summary.status}. {summary.games} games finished; "
        f"{summary.calls} paid-or-probe calls; snapshots {shown or 'none'}; {summary.quotes:,} "
        f"quotes, {summary.new_snapshots:,} new rows, {summary.unmatched_names} unmatched names; "
        f"{summary.credits_used:,} credits"
        + (
            f" ({summary.credits_remaining:,} left on the plan)."
            if summary.credits_remaining is not None
            else "."
        )
    )
    if summary.stopped:
        lines.append(f"Stopped early ({summary.stopped}). Re-run the same command to continue.")
    lines += [f"  error: {e['game']}: {e['error']}" for e in summary.errors]
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    session: Session | None = None,
    provider: OddsProvider | None = None,
    nflverse: NflverseSource | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    config = build_config(settings, args)

    engine = get_engine() if session is None else None
    with backfill_lock(engine) if engine is not None else _no_lock() as got:
        if not got:
            print("Another backfill is running; try again when it has finished.")
            return 0
        with _session(session) as db:
            if not args.skip_nflverse and not args.report:
                load_prerequisites(db, nflverse or NflreadpySource(CACHE_DIR), args.seasons)

            offline = Backfill(db, OfflineProvider(), config, sleep=sleep)
            if args.report:
                print(format_report(offline.report(args.seasons)))
                return 0

            estimate = offline.estimate(args.seasons, args.weeks)
            print(plan.format_estimate(estimate, max_rps=config.max_rps))
            if estimate.games == 0:
                where = "seasons and weeks" if args.weeks else "seasons"
                hint = " Remove --skip-nflverse to load them." if args.skip_nflverse else ""
                print(f"\nNo games found for those {where}.{hint}")
                return 1
            if not args.confirm:
                print("\nNothing was spent. Add --confirm to buy these snapshots.")
                return 0

            cap = args.max_credits if args.max_credits is not None else estimate.credits
            live = provider
            if live is None:
                try:
                    live = get_odds_provider(settings, credit_budget=cap)
                except ValueError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
            try:
                return _spend(db, live, config, args, cap, sleep, estimate)
            finally:
                close = getattr(live, "close", None)
                if provider is None and callable(close):
                    close()


def _spend(
    db: Session,
    live: OddsProvider,
    config: BackfillConfig,
    args: argparse.Namespace,
    cap: int,
    sleep: Callable[[float], None],
    estimate: plan.Estimate,
) -> int:
    backfill = Backfill(db, live, config, sleep=sleep)
    print(f"\nSpending up to {cap:,} credits, keeping {config.reserve:,} on the plan.")

    if args.probe:
        return _probe(db, live, backfill, args.seasons[0])

    link: LinkReport | None = None
    summary = BackfillSummary()
    try:
        if estimate.unlinked_games:
            link = backfill.link_events(args.seasons, args.weeks)
        summary = backfill.run(args.seasons, args.weeks)
    except StopRunError as stop:
        print(f"Stopped early ({stop.reason}). Re-run the same command to continue.")
        return 0
    except OddsProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(summary_text(summary, link))
    print()
    print(format_report(backfill.report(args.seasons)))
    return 1 if summary.errors or summary.status == "failed" else 0


def _probe(db: Session, live: OddsProvider, backfill: Backfill, season: int) -> int:
    """One historical-events call (at most 1 credit) to learn whether history is available."""
    games = backfill.games([season])
    if not games:
        print("No games loaded for that season.", file=sys.stderr)
        return 1
    kickoff = games[0].kickoff_utc
    try:
        events = live.list_historical_events(
            kickoff - timedelta(days=1), kickoff - timedelta(hours=2), kickoff + timedelta(hours=2)
        )
    except OddsProviderError as exc:
        print(f"Historical data is not available on this key: {exc}")
        return 1
    noun = "event" if len(events) == 1 else "events"
    print(f"Historical data works: {len(events)} {noun} listed around {kickoff:%Y-%m-%d %H:%M}Z.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
