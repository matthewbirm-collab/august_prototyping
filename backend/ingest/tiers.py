"""Polling tiers: how often a game's props are captured as kickoff nears. Pure; no I/O.

A tier applies while a game is more than `min_hours_to_kickoff` away, so with the defaults
(CLAUDE.md): more than 72h out is polled every 12h, 72-24h every 3h, 24-3h hourly and the last
3 hours every 15 minutes. Polling stops at kickoff.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.settings import PollTier

# A job that ticks every few minutes shouldn't miss a tier by seconds of jitter.
DUE_SLACK = timedelta(minutes=2)


def interval_label(minutes: int) -> str:
    """12h, 3h, 1h, 15m: the name a tier goes by on the command line."""
    return f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"


def parse_interval(label: str) -> int:
    try:
        number = int(label[:-1])
    except ValueError:
        number = 0
    if label[-1:] not in {"h", "m"} or number <= 0:
        raise ValueError(f"bad tier {label!r}: use auto, or an interval like 15m, 1h, 3h")
    return number * 60 if label.endswith("h") else number


def hours_to_kickoff(kickoff: datetime, now: datetime) -> float:
    return (kickoff - now).total_seconds() / 3600


def tier_for(hours: float, tiers: Sequence[PollTier]) -> PollTier | None:
    """The tier for a game `hours` from kickoff; None once it has kicked off."""
    if hours <= 0:
        return None
    ordered = sorted(tiers, key=lambda t: t.min_hours_to_kickoff, reverse=True)
    for tier in ordered:
        if hours > tier.min_hours_to_kickoff:
            return tier
    return ordered[-1]  # closer than the last threshold: the fastest tier


def is_due(
    now: datetime,
    kickoff: datetime,
    last_polled: datetime | None,
    tiers: Sequence[PollTier],
    *,
    slack: timedelta = DUE_SLACK,
) -> bool:
    tier = tier_for(hours_to_kickoff(kickoff, now), tiers)
    if tier is None:
        return False
    if last_polled is None:
        return True
    interval = timedelta(minutes=tier.interval_minutes)
    return now - last_polled >= interval - min(slack, interval / 4)


def count_polls(now: datetime, kickoff: datetime, tiers: Sequence[PollTier]) -> int:
    """How many polls a game gets from `now` to kickoff if every poll lands on time."""
    polls, moment = 0, now
    while (tier := tier_for(hours_to_kickoff(kickoff, moment), tiers)) is not None:
        polls += 1
        moment += timedelta(minutes=tier.interval_minutes)
    return polls


def region_count(regions: str, bookmakers: Sequence[str] | None) -> int:
    """Every 10 bookmakers count as one region, and `bookmakers` replaces `regions`."""
    return math.ceil(len(bookmakers) / 10) if bookmakers else len(regions.split(","))


def credits_per_poll(markets: Sequence[str], regions: str, bookmakers: Sequence[str] | None) -> int:
    return len(markets) * region_count(regions, bookmakers)


@dataclass(frozen=True)
class Projection:
    days: int
    games: int
    polls: int
    credits_per_poll: int
    credits: int
    budget: int
    games_within_budget: int


def project_spend(
    kickoffs: Sequence[datetime],
    now: datetime,
    tiers: Sequence[PollTier],
    credits_per_poll: int,
    daily_budget: int,
    days: int,
) -> Projection:
    """Credits needed to poll every game on schedule until it kicks off, against the budget."""
    budget = daily_budget * days
    polls = spent = within = 0
    for kickoff in sorted(kickoffs):
        game_polls = count_polls(now, kickoff, tiers)
        polls += game_polls
        spent += game_polls * credits_per_poll
        if spent <= budget:
            within += 1
    return Projection(
        days=days,
        games=len(kickoffs),
        polls=polls,
        credits_per_poll=credits_per_poll,
        credits=polls * credits_per_poll,
        budget=budget,
        games_within_budget=within,
    )


def format_projection(projection: Projection, daily_budget: int) -> str:
    lines = [
        f"Projected spend for the next {projection.days} days: {projection.games} games, "
        f"{projection.polls} polls x {projection.credits_per_poll} credits = "
        f"{projection.credits:,} credits.",
        f"Budget: {daily_budget}/day = {projection.budget:,} over {projection.days} days.",
    ]
    if projection.credits <= projection.budget:
        lines.append("Within budget.")
    else:
        lines.append(
            f"Over budget by {projection.credits - projection.budget:,} credits: the budget "
            f"covers the full schedule of the first {projection.games_within_budget} of "
            f"{projection.games} games. Fewer --markets, longer POLL_TIERS intervals, or a "
            "bigger plan would bring it down."
        )
    return "\n".join(lines)
