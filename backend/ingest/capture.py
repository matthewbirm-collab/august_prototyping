"""Capture live prop snapshots from the odds provider on a tiered schedule.

One run: link this week's games to provider events (free), pick the games that are due for their
tier, request props one game per call, resolve player names, store the quotes that changed, and
record the run in `ingestion_runs`. A game that fails doesn't stop the others; a spent credit
budget does (the remaining games are deferred to the next run).
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.settings import PollTier, Settings
from db.models import Book, Game, IngestionRun, Market
from db.snapshots import SnapshotQuote, store_prop_snapshots
from ingest import tiers as tier_logic
from ingest.nflverse.odds_match import match_odds_events
from ingest.odds.provider import (
    CreditBudgetExceeded,
    CreditUsage,
    OddsProvider,
    OddsProviderError,
    PropQuote,
)
from ingest.resolver import GameContext, PlayerResolver, position_hint_for_market
from ingest.rosters import DbRosterLookup, RosterLookup

logger = logging.getLogger(__name__)

RunStatus = Literal["ok", "partial", "failed", "idle", "budget_exhausted"]
RecomputeHook = Callable[[Sequence[int]], None]
RUN_KIND = "odds_live"


def log_recompute(player_ids: Sequence[int]) -> None:
    """Placeholder for Phase 3, which recomputes consensus and projections for these players."""
    logger.info("recompute requested", extra={"players": len(player_ids)})


@dataclass(frozen=True)
class CaptureConfig:
    source: str
    markets: tuple[str, ...]
    regions: str
    tiers: tuple[PollTier, ...]
    fuzzy_threshold: float
    horizon_days: int
    bookmakers: tuple[str, ...] | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        markets: Sequence[str],
        bookmakers: Sequence[str] | None = None,
    ) -> "CaptureConfig":
        return cls(
            source=settings.odds_provider,
            markets=tuple(markets),
            regions=settings.odds_api_regions,
            tiers=tuple(settings.poll_tiers),
            fuzzy_threshold=settings.fuzzy_match_threshold,
            horizon_days=settings.poll_horizon_days,
            bookmakers=tuple(bookmakers) if bookmakers else None,
        )

    @property
    def credits_per_poll(self) -> int:
        return tier_logic.credits_per_poll(self.markets, self.regions, self.bookmakers)


@dataclass
class RunSummary:
    status: RunStatus
    run_id: int | None = None
    games_polled: int = 0
    quotes: int = 0
    new_snapshots: int = 0
    unmatched_names: int = 0
    credits_used: int = 0
    credits_remaining: int | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    unlinked_games: int = 0


@dataclass
class _GameResult:
    quotes: int = 0
    new_snapshots: int = 0
    unmatched: set[str] = field(default_factory=set)
    one_sided: int = 0
    unknown_market: int = 0
    new_books: list[str] = field(default_factory=list)
    player_ids: set[int] = field(default_factory=set)


def credits_used_today(session: Session, now: datetime) -> int:
    """Credits spent by live odds runs since 00:00 UTC, so the budget survives restarts."""
    midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    total = session.scalar(
        select(func.coalesce(func.sum(IngestionRun.credits_used), 0)).where(
            IngestionRun.kind == RUN_KIND, IngestionRun.started_at >= midnight
        )
    )
    return int(total or 0)


def has_previous_runs(session: Session) -> bool:
    return (
        session.scalar(select(IngestionRun.id).where(IngestionRun.kind == RUN_KIND).limit(1))
        is not None
    )


class OddsCapture:
    def __init__(
        self,
        session: Session,
        provider: OddsProvider,
        config: CaptureConfig,
        *,
        rosters: RosterLookup | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_new_snapshots: RecomputeHook = log_recompute,
    ) -> None:
        self._session = session
        self._provider = provider
        self._cfg = config
        self._clock = clock
        self._on_new_snapshots = on_new_snapshots
        self._resolver = PlayerResolver(
            session,
            rosters or DbRosterLookup(session),
            source=config.source,
            fuzzy_threshold=config.fuzzy_threshold,
        )

    # Selecting games --------------------------------------------------------------------------

    def upcoming_games(self, now: datetime) -> list[Game]:
        """Games kicking off within the horizon, soonest first."""
        horizon = now + timedelta(days=self._cfg.horizon_days)
        return list(
            self._session.scalars(
                select(Game)
                .where(Game.kickoff_utc > now, Game.kickoff_utc <= horizon)
                .order_by(Game.kickoff_utc, Game.id)
            )
        )

    def last_polled(self, now: datetime) -> dict[str, datetime]:
        """When each game was last polled, from earlier runs' records."""
        longest = max(t.interval_minutes for t in self._cfg.tiers)
        since = now - timedelta(minutes=longest) - timedelta(hours=1)
        runs = self._session.execute(
            select(IngestionRun.details).where(
                IngestionRun.kind == RUN_KIND,
                IngestionRun.status.in_(("ok", "partial")),
                IngestionRun.started_at >= since,
            )
        )
        polled: dict[str, datetime] = {}
        for (details,) in runs:
            for game_id, info in (details or {}).get("games", {}).items():
                moment = datetime.fromisoformat(info["polled_at"])
                if game_id not in polled or moment > polled[game_id]:
                    polled[game_id] = moment
        return polled

    def due_games(self, now: datetime, tier: str = "auto") -> tuple[list[Game], int]:
        """(games to poll now, upcoming games not yet linked to a provider event)."""
        forced = self._forced_interval(tier)
        upcoming = self.upcoming_games(now)
        linked = [g for g in upcoming if g.odds_event_id]
        last = self.last_polled(now)
        due = [g for g in linked if self._is_due(g, now, last.get(g.id), forced)]
        return due, len(upcoming) - len(linked)

    def _forced_interval(self, tier: str) -> int | None:
        if tier == "auto":
            return None
        minutes = tier_logic.parse_interval(tier)
        configured = {t.interval_minutes for t in self._cfg.tiers}
        if minutes not in configured:
            names = ", ".join(tier_logic.interval_label(m) for m in sorted(configured))
            raise ValueError(f"no polling tier {tier!r}; configured tiers: {names}")
        return minutes

    def _is_due(self, game: Game, now: datetime, last: datetime | None, forced: int | None) -> bool:
        if forced is None:
            return tier_logic.is_due(now, game.kickoff_utc, last, self._cfg.tiers)
        current = tier_logic.tier_for(
            tier_logic.hours_to_kickoff(game.kickoff_utc, now), self._cfg.tiers
        )
        return current is not None and current.interval_minutes == forced

    # Running ----------------------------------------------------------------------------------

    def run(self, tier: str = "auto") -> RunSummary:
        """Poll every game that is due. Commits as it goes."""
        now = self._clock()
        self._forced_interval(tier)  # reject a bad --tier before spending anything
        self._link_events(now)
        due, unlinked = self.due_games(now, tier)
        summary = RunSummary(status="idle", unlinked_games=unlinked)
        if not due:
            return summary

        before = self._provider.credit_usage()
        if self._cannot_afford_one_poll(before):
            summary.status = "budget_exhausted"
            summary.deferred = [g.id for g in due]
            logger.warning(
                "odds budget spent; skipping run",
                extra={"used_today": before.used_today, "daily_budget": before.daily_budget},
            )
            return summary

        run = IngestionRun(kind=RUN_KIND, details={"tier": tier})
        self._session.add(run)
        self._session.commit()
        summary.run_id = run.id

        games_detail: dict[str, dict[str, Any]] = {}
        totals = _GameResult()
        for index, game in enumerate(due):
            polled_at = self._clock()
            try:
                response = self._provider.get_event_props(
                    str(game.odds_event_id), self._cfg.markets, self._cfg.bookmakers
                )
            except CreditBudgetExceeded as exc:
                summary.deferred = [g.id for g in due[index:]]
                summary.errors.append({"game": game.id, "error": f"CreditBudgetExceeded: {exc}"})
                break
            except OddsProviderError as exc:
                logger.warning("odds poll failed", extra={"game": game.id, "error": str(exc)})
                summary.errors.append({"game": game.id, "error": f"OddsProviderError: {exc}"})
                continue

            try:
                with self._session.begin_nested():
                    result = self._store(game, response.quotes, run.id)
                self._session.commit()
            except Exception as exc:
                logger.exception("storing odds failed", extra={"game": game.id})
                summary.errors.append({"game": game.id, "error": f"{type(exc).__name__}: {exc}"})
                continue

            games_detail[game.id] = {
                "event_id": game.odds_event_id,
                "polled_at": polled_at.isoformat(),
                "quotes": result.quotes,
                "new_snapshots": result.new_snapshots,
                "unmatched_names": sorted(result.unmatched),
            }
            summary.games_polled += 1
            summary.quotes += result.quotes
            summary.new_snapshots += result.new_snapshots
            summary.unmatched_names += len(result.unmatched)
            totals.one_sided += result.one_sided
            totals.unknown_market += result.unknown_market
            totals.new_books.extend(result.new_books)
            self._notify(result.player_ids)

        after = self._provider.credit_usage()
        summary.credits_used = after.used_today - before.used_today
        summary.credits_remaining = after.remaining
        failed = bool(summary.errors or summary.deferred)
        summary.status = "ok" if not failed else ("partial" if summary.games_polled else "failed")
        self._finish(run, summary, games_detail, totals)
        return summary

    # Pieces -----------------------------------------------------------------------------------

    def _link_events(self, now: datetime) -> None:
        """Keep games linked to provider events. Listing events is free."""
        try:
            events = self._provider.list_events(now, now + timedelta(days=self._cfg.horizon_days))
            match_odds_events(self._session, events)
            self._session.commit()
        except OddsProviderError as exc:
            self._session.rollback()
            logger.warning("could not refresh odds events", extra={"error": str(exc)})

    def _cannot_afford_one_poll(self, usage: CreditUsage) -> bool:
        cost = self._cfg.credits_per_poll
        if usage.used_today + cost > usage.daily_budget:
            return True
        return usage.remaining is not None and cost > usage.remaining

    def _store(self, game: Game, quotes: Sequence[PropQuote], run_id: int) -> _GameResult:
        result = _GameResult(quotes=len(quotes))
        known_markets = set(self._session.scalars(select(Market.key)))
        result.new_books = self._ensure_books({q.book for q in quotes})
        resolver = self._resolver.for_game(GameContext.from_game(game))

        prices: dict[tuple[str, str, int, float | None], dict[str, int]] = {}
        captured: dict[tuple[str, str, int, float | None], datetime] = {}
        for quote in quotes:
            if quote.market not in known_markets:
                result.unknown_market += 1
                continue
            resolution = resolver.resolve(
                quote.player_name_raw, position_hint=position_hint_for_market(quote.market)
            )
            if resolution.player_id is None:
                result.unmatched.add(quote.player_name_raw)
                continue
            key = (quote.book, quote.market, resolution.player_id, quote.line)
            prices.setdefault(key, {})[quote.side] = quote.price
            captured[key] = quote.captured_at

        snapshots: list[SnapshotQuote] = []
        for key, sides in prices.items():
            book, market, player_id, line = key
            over = sides.get("over", sides.get("yes"))
            if over is None:
                result.one_sided += 1  # only an under/no price: nothing to anchor the row on
                continue
            snapshots.append(
                SnapshotQuote(
                    captured_at=captured[key],
                    game_id=game.id,
                    book_key=book,
                    market_key=market,
                    player_id=player_id,
                    line=line,
                    over_price=over,
                    under_price=sides.get("under", sides.get("no")),
                    source="live",
                    ingestion_run_id=run_id,
                )
            )
        stored = store_prop_snapshots(self._session, snapshots)
        result.new_snapshots = len(stored)
        result.player_ids = {q.player_id for q in stored}
        return result

    def _ensure_books(self, keys: set[str]) -> list[str]:
        """Register a book the provider returns that we haven't seeded (default weight)."""
        known = set(self._session.scalars(select(Book.key).where(Book.key.in_(keys))))
        new = sorted(keys - known)
        if new:
            logger.warning("new sportsbook from provider", extra={"books": new})
            self._session.execute(
                insert(Book)
                .values([{"key": key, "display_name": key} for key in new])
                .on_conflict_do_nothing()
            )
        return new

    def _notify(self, player_ids: set[int]) -> None:
        if not player_ids:
            return
        try:
            self._on_new_snapshots(sorted(player_ids))
        except Exception:
            logger.exception("recompute hook failed")

    def _finish(
        self,
        run: IngestionRun,
        summary: RunSummary,
        games: dict[str, dict[str, Any]],
        totals: _GameResult,
    ) -> None:
        run.finished_at = datetime.now(UTC)
        run.status = summary.status
        run.events = summary.games_polled
        run.quotes = summary.quotes
        run.new_snapshots = summary.new_snapshots
        run.credits_used = summary.credits_used
        run.credits_remaining = summary.credits_remaining
        run.errors = summary.errors
        run.details = {
            "tier": run.details.get("tier", "auto"),
            "games": games,
            "deferred": summary.deferred,
            "unmatched_names": summary.unmatched_names,
            "one_sided_quotes": totals.one_sided,
            "unknown_market_quotes": totals.unknown_market,
            "new_books": sorted(set(totals.new_books)),
        }
        self._session.commit()
