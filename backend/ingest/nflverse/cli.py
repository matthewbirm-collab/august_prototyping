"""Load nflverse data.

    uv run ingest-nflverse --season 2026 --week 8 --datasets injuries,depth_charts
    uv run ingest-nflverse --season 2026                  # everything, whole season
    uv run ingest-nflverse --season 2026 --week 8 --match-odds

`--match-odds` links that week's games to The Odds API events. Listing events is free, but it
needs ODDS_API_KEY.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.logging import configure_logging
from app.settings import get_settings
from db.models import Game
from db.session import get_engine
from ingest.nflverse.load import DATASETS, NflverseLoader
from ingest.nflverse.odds_match import match_odds_events
from ingest.nflverse.source import NflreadpySource, NflverseSource
from ingest.odds.factory import get_odds_provider
from ingest.odds.provider import OddsProvider

CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "nflverse"


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ingest-nflverse", description=__doc__.splitlines()[0])
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--week", type=int)
    parser.add_argument(
        "--datasets",
        default=",".join(DATASETS),
        help=f"comma-separated; any of {', '.join(DATASETS)} (default: all)",
    )
    parser.add_argument("--match-odds", action="store_true", help="link games to odds events")
    args = parser.parse_args(argv)
    args.datasets = [d.strip() for d in args.datasets.split(",") if d.strip()]
    unknown = set(args.datasets) - set(DATASETS)
    if unknown:
        parser.error(f"unknown datasets: {', '.join(sorted(unknown))}")
    if args.match_odds and args.week is None:
        parser.error("--match-odds needs --week")
    return args


def main(
    argv: Sequence[str] | None = None,
    *,
    session: Session | None = None,
    source: NflverseSource | None = None,
    odds: OddsProvider | None = None,
) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    db = session if session is not None else Session(get_engine())
    try:
        loader = NflverseLoader(db, source if source is not None else NflreadpySource(CACHE_DIR))
        counts = loader.run(args.season, args.week, args.datasets)
        for dataset in args.datasets:
            print(f"{dataset}: {counts.get(dataset, 'failed')}")
        provider = (odds or get_odds_provider(settings)) if args.match_odds else None
        if provider is not None and not _match_odds(db, provider, args.season, args.week):
            return 1
        return 0 if len(counts) == len(args.datasets) else 1
    finally:
        if session is None:
            db.close()


def _match_odds(db: Session, odds: OddsProvider, season: int, week: int) -> bool:
    first, last = db.execute(
        select(func.min(Game.kickoff_utc), func.max(Game.kickoff_utc)).where(
            Game.season == season, Game.week == week
        )
    ).one()
    if first is None:
        print(f"No games for {season} week {week}; load schedules first.", file=sys.stderr)
        return False
    events = odds.list_events(first - timedelta(days=1), last + timedelta(days=1))
    report = match_odds_events(db, events)
    db.commit()
    unmatched_games = db.scalar(
        select(func.count())
        .select_from(Game)
        .where(Game.season == season, Game.week == week, Game.odds_event_id.is_(None))
    )
    print(
        f"odds events: {len(report.matched)} matched, {len(report.unmatched_events)} unmatched; "
        f"{unmatched_games} games without an event"
    )
    return True


if __name__ == "__main__":
    sys.exit(main())
