"""Planning for the historical backfill: which snapshots to capture and what they cost. Pure.

Per game the backfill captures up to six snapshots, named by label:

    open   the first snapshot that has props, found by scanning forward from nine days before
           kickoff in six-hour steps (a response with no props is free, so scanning costs
           requests, not credits)
    t72h, t24h, t6h, t1h   the provider's closest snapshot at or before that many hours out
    close  the last snapshot before kickoff (1 minute out, falling back to 10 and 30)

Historical calls cost 10 credits x markets returned x regions (paid plans only), so the
estimate here is an upper bound.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

LABELS = ("open", "t72h", "t24h", "t6h", "t1h", "close")
HOURS_OUT = {"t72h": 72, "t24h": 24, "t6h": 6, "t1h": 1}
CLOSE_MINUTES_OUT = (1, 10, 30)
SCAN_START = timedelta(days=9)
SCAN_STEP = timedelta(hours=6)
HISTORICAL_MULTIPLIER = 10
# Props usually appear about five days out, so a typical open search probes this many times.
TYPICAL_SCAN_PROBES = 16


@dataclass(frozen=True)
class Probe:
    """One historical request. `labels` are the snapshots a non-empty answer satisfies;
    `scan` marks a point of the open search, only needed until props have been seen."""

    at: datetime
    labels: tuple[str, ...] = ()
    scan: bool = False


def close_candidates(kickoff: datetime) -> list[datetime]:
    """Times to try for the close, latest first. Some books pull props shortly before kickoff."""
    return [kickoff - timedelta(minutes=m) for m in CLOSE_MINUTES_OUT]


def sweep(
    kickoff: datetime, pending: Collection[str], scan_step: timedelta = SCAN_STEP
) -> list[Probe]:
    """The probes needed for the `pending` labels, in time order. `close` is handled apart,
    since it has fallback times (see `close_candidates`)."""
    by_time: dict[datetime, Probe] = {}

    def add(at: datetime, label: str | None, scan: bool) -> None:
        existing = by_time.get(at)
        labels = (*(existing.labels if existing else ()), *([label] if label else []))
        by_time[at] = Probe(at, labels, scan or bool(existing and existing.scan))

    for label, hours in HOURS_OUT.items():
        if label in pending:
            add(kickoff - timedelta(hours=hours), label, False)
    if "open" in pending:
        point = kickoff - SCAN_START
        last = kickoff - timedelta(hours=HOURS_OUT["t1h"])
        while point < last:
            add(point, None, True)
            point += scan_step
    return [by_time[at] for at in sorted(by_time)]


def credits_per_call(markets: Sequence[str], regions: int) -> int:
    return HISTORICAL_MULTIPLIER * len(markets) * regions


@dataclass(frozen=True)
class GameInfo:
    game_id: str
    season: int
    week: int
    kickoff: datetime
    linked: bool


@dataclass(frozen=True)
class SeasonEstimate:
    season: int
    games: int
    snapshots: int
    done: int
    calls: int
    credits: int


@dataclass(frozen=True)
class Estimate:
    seasons: tuple[SeasonEstimate, ...]
    credits_per_call: int
    link_credits: int
    link_credits_worst: int
    typical_requests: int
    unlinked_games: int = 0
    labels: tuple[str, ...] = field(default=LABELS)

    @property
    def games(self) -> int:
        return sum(s.games for s in self.seasons)

    @property
    def snapshots(self) -> int:
        return sum(s.snapshots for s in self.seasons)

    @property
    def done(self) -> int:
        return sum(s.done for s in self.seasons)

    @property
    def calls(self) -> int:
        return sum(s.calls for s in self.seasons)

    @property
    def snapshot_credits(self) -> int:
        return sum(s.credits for s in self.seasons)

    @property
    def credits(self) -> int:
        """Upper bound: snapshot calls plus linking games to historical events."""
        return self.snapshot_credits + self.link_credits_worst


def estimate_backfill(
    games: Sequence[GameInfo],
    done: Collection[tuple[str, str]],
    labels: Sequence[str],
    per_call: int,
    *,
    unlinked_weeks: int = 0,
    unlinked_games: int = 0,
) -> Estimate:
    """Upper-bound cost of the snapshots not yet captured for `games`.

    Linking costs one credit per week of unlinked games, plus one per game the weekly pass
    misses (`link_credits_worst`).
    """
    per_season: dict[int, list[int]] = {}
    typical = 0
    for game in games:
        counts = per_season.setdefault(game.season, [0, 0, 0])  # games, snapshots, done
        counts[0] += 1
        counts[1] += len(labels)
        pending = [label for label in labels if (game.game_id, label) not in done]
        counts[2] += len(labels) - len(pending)
        typical += len(pending) + (TYPICAL_SCAN_PROBES if "open" in pending else 0)

    seasons = tuple(
        SeasonEstimate(
            season=season,
            games=c[0],
            snapshots=c[1],
            done=c[2],
            calls=c[1] - c[2],
            credits=(c[1] - c[2]) * per_call,
        )
        for season, c in sorted(per_season.items())
    )
    return Estimate(
        seasons=seasons,
        credits_per_call=per_call,
        link_credits=unlinked_weeks,
        link_credits_worst=unlinked_weeks + unlinked_games,
        typical_requests=typical,
        unlinked_games=unlinked_games,
        labels=tuple(labels),
    )


def format_estimate(estimate: Estimate, *, max_rps: float) -> str:
    lines = [
        f"Backfill estimate: {estimate.games} games x {len(estimate.labels)} snapshots "
        f"({', '.join(estimate.labels)}), {estimate.credits_per_call} credits per call.",
        "",
        f"  {'season':<8}{'games':>7}{'snapshots':>11}{'done':>7}{'to buy':>9}"
        f"{'credits (up to)':>17}",
    ]
    for s in estimate.seasons:
        lines.append(
            f"  {s.season:<8}{s.games:>7}{s.snapshots:>11}{s.done:>7}{s.calls:>9}{s.credits:>17,}"
        )
    lines += [
        f"  {'total':<8}{estimate.games:>7}{estimate.snapshots:>11}{estimate.done:>7}"
        f"{estimate.calls:>9}{estimate.snapshot_credits:>17,}",
        "",
        f"Linking games to historical events: {estimate.link_credits} credits "
        f"(up to {estimate.link_credits_worst}).",
        f"Total: up to {estimate.credits:,} credits. Markets that return nothing are not charged, "
        "so the real figure is usually lower.",
    ]
    seconds = estimate.typical_requests / max(max_rps, 0.01)
    duration = (
        f"{seconds / 3600:.1f} hours" if seconds >= 3600 else f"{max(seconds / 60, 1):.0f} minutes"
    )
    lines.append(
        f"About {estimate.typical_requests:,} requests (most are free empty probes from the open "
        f"search): roughly {duration} at {max_rps:g} requests a second."
    )
    return "\n".join(lines)
