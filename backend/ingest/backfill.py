"""Historical backfill of prop snapshots for past regular seasons. See `backfill_plan` for what
is captured and how it is priced.

The run is resumable: every finished snapshot of every game (including empty and stale ones) is
recorded in `backfill_progress`, so a rerun only buys what is missing. Games are processed in
kickoff order and each game's snapshots in time order, so deduplication compares each snapshot
with the one before it.
"""

import logging
import time
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import BackfillProgress, Game, IngestionRun
from ingest import backfill_plan as plan
from ingest.nflverse.odds_match import match_odds_events
from ingest.odds.provider import (
    CreditBudgetExceeded,
    OddsEvent,
    OddsProvider,
    OddsProviderError,
    PropsResponse,
)
from ingest.quote_store import GameResult, QuoteStore
from ingest.resolver import (
    PlayerResolver,
    UnmatchedRate,
    season_unmatched_rate,
)
from ingest.rosters import DbRosterLookup, RosterLookup
from ingest.tiers import region_count

logger = logging.getLogger(__name__)

RUN_KIND = "odds_historical"
Status = Literal["done", "empty", "stale"]


@dataclass(frozen=True)
class BackfillConfig:
    source: str
    markets: tuple[str, ...]
    regions: str
    fuzzy_threshold: float
    stale_after: timedelta
    labels: tuple[str, ...] = plan.LABELS
    bookmakers: tuple[str, ...] | None = None
    scan_step: timedelta = plan.SCAN_STEP
    max_rps: float = 2.0
    reserve: int = 1000

    @property
    def per_call(self) -> int:
        return plan.credits_per_call(self.markets, region_count(self.regions, self.bookmakers))


@dataclass
class BackfillSummary:
    status: Literal["ok", "partial", "failed", "idle"] = "idle"
    run_id: int | None = None
    games: int = 0
    calls: int = 0
    snapshots: Counter[str] = field(default_factory=Counter)
    quotes: int = 0
    new_snapshots: int = 0
    credits_used: int = 0
    credits_remaining: int | None = None
    unmatched_names: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    stopped: str | None = None


@dataclass
class LinkReport:
    weeks: int = 0
    linked: int = 0
    unlinked: list[str] = field(default_factory=list)
    credits: int = 0


@dataclass(frozen=True)
class SeasonReport:
    season: int
    games: int
    linked: int
    statuses: dict[str, int]
    credits: int
    quotes: int
    new_snapshots: int
    unmatched: UnmatchedRate


class StopRunError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Fetch:
    response: PropsResponse
    reused: bool
    credits: int


class _Throttle:
    def __init__(
        self, rps: float, monotonic: Callable[[], float], sleep: Callable[[float], None]
    ) -> None:
        self._gap = 1 / rps if rps > 0 else 0.0
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self._monotonic()
        if self._last is not None and now - self._last < self._gap:
            self._sleep(self._gap - (now - self._last))
            now = self._monotonic()
        self._last = now


class Backfill:
    def __init__(
        self,
        session: Session,
        provider: OddsProvider,
        config: BackfillConfig,
        *,
        rosters: RosterLookup | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._provider = provider
        self._cfg = config
        self._throttle = _Throttle(config.max_rps, monotonic, sleep)
        self._quotes = QuoteStore(
            session,
            PlayerResolver(
                session,
                rosters or DbRosterLookup(session),
                source=config.source,
                fuzzy_threshold=config.fuzzy_threshold,
            ),
            "historical",
        )

    # Selecting and estimating -----------------------------------------------------------------

    def games(self, seasons: Sequence[int], weeks: Collection[int] | None = None) -> list[Game]:
        stmt = select(Game).where(Game.season.in_(seasons), Game.season_type == "REG")
        if weeks:
            stmt = stmt.where(Game.week.in_(weeks))
        return list(self._session.scalars(stmt.order_by(Game.kickoff_utc, Game.id)))

    def _done(self, games: Sequence[Game]) -> set[tuple[str, str]]:
        ids = [g.id for g in games]
        if not ids:
            return set()
        rows = self._session.execute(
            select(BackfillProgress.game_id, BackfillProgress.label).where(
                BackfillProgress.game_id.in_(ids)
            )
        )
        return {(game_id, label) for game_id, label in rows}

    def estimate(
        self, seasons: Sequence[int], weeks: Collection[int] | None = None
    ) -> plan.Estimate:
        games = self.games(seasons, weeks)
        unlinked = [g for g in games if not g.odds_event_id]
        return plan.estimate_backfill(
            [
                plan.GameInfo(g.id, g.season, g.week, g.kickoff_utc, bool(g.odds_event_id))
                for g in games
            ],
            self._done(games),
            self._cfg.labels,
            self._cfg.per_call,
            unlinked_weeks=len({(g.season, g.week) for g in unlinked}),
            unlinked_games=len(unlinked),
        )

    # Linking games to historical events -------------------------------------------------------

    def link_events(
        self, seasons: Sequence[int], weeks: Collection[int] | None = None
    ) -> LinkReport:
        """Find each past game's provider event id. One historical-events call per week of
        games; games it misses get one call of their own. Costs 1 credit per call that
        returns events."""
        report = LinkReport()
        start = self._provider.credit_usage().used_today
        unlinked = [g for g in self.games(seasons, weeks) if not g.odds_event_id]
        by_week: dict[tuple[int, int], list[Game]] = {}
        for game in unlinked:
            by_week.setdefault((game.season, game.week), []).append(game)

        pad = timedelta(hours=2)
        for group in by_week.values():
            first, last = group[0].kickoff_utc, group[-1].kickoff_utc
            self._link(self._call_events(first - timedelta(days=1), first - pad, last + pad))
            report.weeks += 1

        for game in unlinked:
            if not game.odds_event_id:  # the weekly calls above may have linked it
                at = game.kickoff_utc - timedelta(hours=6)
                window = (game.kickoff_utc - pad, game.kickoff_utc + pad)
                self._link(self._call_events(at, *window))
            if not game.odds_event_id:
                report.unlinked.append(game.id)
        report.linked = len(unlinked) - len(report.unlinked)
        report.credits = self._provider.credit_usage().used_today - start
        return report

    def _call_events(self, at: datetime, start: datetime, end: datetime) -> list[OddsEvent]:
        self._throttle.wait()
        try:
            return self._provider.list_historical_events(at, start, end)
        except CreditBudgetExceeded as exc:
            raise StopRunError("credit cap") from exc

    def _link(self, events: Sequence[OddsEvent]) -> None:
        match_odds_events(self._session, events)
        self._session.commit()

    # Running ----------------------------------------------------------------------------------

    def run(self, seasons: Sequence[int], weeks: Collection[int] | None = None) -> BackfillSummary:
        """Capture every snapshot not yet in `backfill_progress`. Commits as it goes."""
        games = [g for g in self.games(seasons, weeks) if g.odds_event_id]
        done = self._done(games)
        todo = [g for g in games if any((g.id, label) not in done for label in self._cfg.labels)]
        summary = BackfillSummary()
        if not todo:
            return summary

        run = IngestionRun(
            kind=RUN_KIND,
            details={
                "seasons": list(seasons),
                "weeks": sorted(weeks) if weeks else None,
                "labels": list(self._cfg.labels),
                "markets": list(self._cfg.markets),
            },
        )
        self._session.add(run)
        self._session.commit()
        summary.run_id = run.id
        before = self._provider.credit_usage().used_today
        unmatched: set[tuple[str, str]] = set()

        for game in todo:
            pending = [label for label in self._cfg.labels if (game.id, label) not in done]
            try:
                self._process_game(game, pending, run.id, summary, unmatched)
            except StopRunError as stop:
                summary.stopped = stop.reason
                break
            except OddsProviderError as exc:
                logger.warning("backfill poll failed", extra={"game": game.id, "error": str(exc)})
                summary.errors.append({"game": game.id, "error": f"OddsProviderError: {exc}"})
            except Exception as exc:
                logger.exception("backfill failed", extra={"game": game.id})
                self._session.rollback()
                summary.errors.append({"game": game.id, "error": f"{type(exc).__name__}: {exc}"})
            else:
                summary.games += 1

        usage = self._provider.credit_usage()
        summary.credits_used = usage.used_today - before
        summary.credits_remaining = usage.remaining
        summary.unmatched_names = len(unmatched)
        made_progress = sum(summary.snapshots.values()) > 0
        problem = bool(summary.errors or summary.stopped)
        summary.status = "ok" if not problem else ("partial" if made_progress else "failed")
        self._finish(run, summary)
        return summary

    def _process_game(
        self,
        game: Game,
        pending: list[str],
        run_id: int,
        summary: BackfillSummary,
        unmatched: set[tuple[str, str]],
    ) -> None:
        kickoff = game.kickoff_utc
        opened = "open" not in pending
        last: PropsResponse | None = None

        def handle(at: datetime, labels: list[str], fetch: _Fetch) -> None:
            nonlocal opened
            response = fetch.response
            if not response.quotes:
                for label in labels:
                    self._record(
                        game, label, at, response, "empty", GameResult(), 0, run_id, summary
                    )
                return
            attach = list(labels)
            if not opened and "open" in pending:
                attach.insert(0, "open")
            opened = True
            result = GameResult()
            if not fetch.reused:
                with self._session.begin_nested():
                    result = self._quotes.store(game, response.quotes, run_id)
                unmatched.update((game.id, name) for name in result.unmatched)
            spent = fetch.credits
            for label in attach:
                stale = label != "open" and at - response.snapshot_at > self._cfg.stale_after
                self._record(
                    game,
                    label,
                    at,
                    response,
                    "stale" if stale else "done",
                    result,
                    spent,
                    run_id,
                    summary,
                )
                spent = 0  # one call is billed once, whichever labels it satisfies
                result = GameResult(quotes=0)

        for probe in plan.sweep(kickoff, pending, self._cfg.scan_step):
            if probe.scan and not probe.labels and opened:
                continue
            fetch = self._fetch(game, probe.at, last, summary)
            last = fetch.response
            handle(probe.at, list(probe.labels), fetch)

        if "close" in pending:
            candidates = plan.close_candidates(kickoff)
            for at in candidates:
                fetch = self._fetch(game, at, last, summary)
                last = fetch.response
                if fetch.response.quotes:
                    handle(at, ["close"], fetch)
                    break
            else:
                nothing = PropsResponse((), candidates[0])
                handle(candidates[0], ["close"], _Fetch(nothing, True, 0))

        if "open" in pending and not opened:
            # Props never appeared in the search window: record it so reruns don't search again.
            at = kickoff - plan.SCAN_START
            self._record(
                game, "open", at, PropsResponse((), at), "empty", GameResult(), 0, run_id, summary
            )

    def _fetch(
        self, game: Game, at: datetime, last: PropsResponse | None, summary: BackfillSummary
    ) -> _Fetch:
        """The snapshot at or before `at`, without a request if the previous answer covers it."""
        if (
            last is not None
            and last.next_snapshot_at is not None
            and last.snapshot_at <= at < last.next_snapshot_at
        ):
            return _Fetch(last, True, 0)

        self._throttle.wait()
        usage = self._provider.credit_usage()
        if usage.remaining is not None and usage.remaining - self._cfg.per_call < self._cfg.reserve:
            raise StopRunError("reserve")
        try:
            response = self._provider.get_historical_event_props(
                str(game.odds_event_id), at, self._cfg.markets, self._cfg.bookmakers
            )
        except CreditBudgetExceeded as exc:
            raise StopRunError("credit cap") from exc
        summary.calls += 1
        spent = self._provider.credit_usage().used_today - usage.used_today
        return _Fetch(response, False, spent)

    def _record(
        self,
        game: Game,
        label: str,
        target: datetime,
        response: PropsResponse,
        status: Status,
        result: GameResult,
        credits: int,
        run_id: int,
        summary: BackfillSummary,
    ) -> None:
        self._session.merge(
            BackfillProgress(
                game_id=game.id,
                label=label,
                target_at=target,
                snapshot_at=response.snapshot_at if response.quotes else None,
                status=status,
                quotes=result.quotes,
                new_snapshots=result.new_snapshots,
                credits=credits,
                ingestion_run_id=run_id,
            )
        )
        self._session.commit()
        summary.snapshots[status] += 1
        summary.quotes += result.quotes
        summary.new_snapshots += result.new_snapshots

    def _finish(self, run: IngestionRun, summary: BackfillSummary) -> None:
        run.finished_at = datetime.now(UTC)
        run.status = summary.status
        run.events = summary.games
        run.quotes = summary.quotes
        run.new_snapshots = summary.new_snapshots
        run.credits_used = summary.credits_used
        run.credits_remaining = summary.credits_remaining
        run.errors = summary.errors
        run.details = {
            **run.details,
            "calls": summary.calls,
            "snapshots": dict(summary.snapshots),
            "unmatched_names": summary.unmatched_names,
            "stopped": summary.stopped,
        }
        self._session.commit()

    # Reporting --------------------------------------------------------------------------------

    def report(self, seasons: Sequence[int]) -> list[SeasonReport]:
        reports = []
        for season in seasons:
            games = self.games([season])
            statuses = Counter[str]()
            credits = quotes = new_snapshots = 0
            rows = self._session.execute(
                select(
                    BackfillProgress.status,
                    func.count(),
                    func.coalesce(func.sum(BackfillProgress.credits), 0),
                    func.coalesce(func.sum(BackfillProgress.quotes), 0),
                    func.coalesce(func.sum(BackfillProgress.new_snapshots), 0),
                )
                .join(Game, Game.id == BackfillProgress.game_id)
                .where(Game.season == season)
                .group_by(BackfillProgress.status)
            )
            for status, count, spent, q, n in rows:
                statuses[status] = count
                credits += int(spent)
                quotes += int(q)
                new_snapshots += int(n)
            reports.append(
                SeasonReport(
                    season=season,
                    games=len(games),
                    linked=sum(1 for g in games if g.odds_event_id),
                    statuses=dict(statuses),
                    credits=credits,
                    quotes=quotes,
                    new_snapshots=new_snapshots,
                    unmatched=season_unmatched_rate(self._session, season),
                )
            )
        return reports


GATE = 0.02


def format_report(reports: Sequence[SeasonReport]) -> str:
    lines = []
    for r in reports:
        s = r.statuses
        lines.append(
            f"{r.season}: {r.games} games ({r.linked} linked to events). Snapshots: "
            f"{s.get('done', 0)} done, {s.get('empty', 0)} empty, {s.get('stale', 0)} stale. "
            f"{r.credits:,} credits, {r.quotes:,} quotes, {r.new_snapshots:,} new rows."
        )
        u = r.unmatched
        verdict = "under" if u.rate < GATE else "OVER"
        lines.append(
            f"      Unmatched players: {u.unmatched} of {u.unmatched + u.matched_players} "
            f"({u.rate:.1%}), {verdict} the {GATE:.0%} gate."
        )
    return "\n".join(lines)
