"""Provider-neutral odds types and the `OddsProvider` interface.

Everything downstream (resolver, scheduler, backfill) depends on this module only, so the odds
provider can be swapped by config (CLAUDE.md, data sources).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

Side = Literal["over", "under", "yes", "no"]


@dataclass(frozen=True)
class OddsEvent:
    """One game as the provider identifies it."""

    event_id: str
    home_team: str
    away_team: str
    commence_time: datetime


@dataclass(frozen=True)
class EventMarkets:
    """Market keys one bookmaker has recently offered for an event."""

    book: str
    markets: tuple[str, ...]


@dataclass(frozen=True)
class PropQuote:
    """One side of one player prop at one book, as the provider sent it.

    `player_name_raw` is free text; the resolver maps it to a canonical player. `line` is None for
    markets without a threshold, such as anytime TD.
    """

    event_id: str
    book: str
    market: str
    player_name_raw: str
    side: Side
    line: float | None
    price: int
    captured_at: datetime


@dataclass(frozen=True)
class PropsResponse:
    """Quotes from one call, plus when the provider says the snapshot was taken.

    Historical responses also say when the snapshots on either side of this one were taken, so a
    caller can tell that a later request would land on the same snapshot.
    """

    quotes: tuple[PropQuote, ...]
    snapshot_at: datetime
    previous_snapshot_at: datetime | None = None
    next_snapshot_at: datetime | None = None


@dataclass(frozen=True)
class CreditUsage:
    """Credit counters as of the provider's latest response."""

    used: int | None
    remaining: int | None
    last_call: int | None
    used_today: int
    daily_budget: int


class OddsProviderError(Exception):
    """The provider returned an error or could not be reached after retries."""


class CreditBudgetExceeded(OddsProviderError):  # noqa: N818  (reads better than ...Error)
    """A call was refused because it could exceed the daily credit budget."""


class OddsProvider(Protocol):
    """Read access to sportsbook odds. Implementations must enforce the daily credit budget."""

    def list_events(self, commence_from: datetime, commence_to: datetime) -> list[OddsEvent]:
        """Games kicking off in [commence_from, commence_to].

        The roadmap describes this as `list_events(season, week)`. Week boundaries come from the
        nflverse schedule (`/p2-nflverse`), so callers pass that week's time window.
        """
        ...

    def list_event_markets(self, event_id: str) -> list[EventMarkets]:
        """Market keys each bookmaker has recently offered for an event."""
        ...

    def get_event_props(
        self,
        event_id: str,
        markets: Sequence[str],
        bookmakers: Sequence[str] | None = None,
    ) -> PropsResponse:
        """Current player-prop quotes for one event."""
        ...

    def get_historical_event_props(
        self,
        event_id: str,
        at: datetime,
        markets: Sequence[str],
        bookmakers: Sequence[str] | None = None,
    ) -> PropsResponse:
        """Player-prop quotes from the provider's closest snapshot at or before `at`."""
        ...

    def list_historical_events(
        self, at: datetime, commence_from: datetime, commence_to: datetime
    ) -> list[OddsEvent]:
        """Events the provider listed as of the snapshot at or before `at`, kicking off within
        [commence_from, commence_to]."""
        ...

    def credit_usage(self) -> CreditUsage: ...
