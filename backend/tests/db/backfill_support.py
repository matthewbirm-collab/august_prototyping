"""A fake historical odds API and a small world of 2025 games for the backfill tests."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from db.models import Game, Player, Roster
from db.seed import seed
from ingest.backfill import BackfillConfig
from ingest.odds.provider import (
    CreditBudgetExceeded,
    CreditUsage,
    EventMarkets,
    OddsEvent,
    OddsProviderError,
    PropQuote,
    PropsResponse,
)

MARKETS = ("player_rush_yds", "player_anytime_td")
PER_CALL = 10 * len(MARKETS)  # credits for a call that returns both markets
KICKOFF_A = datetime(2025, 10, 19, 17, 0, tzinfo=UTC)
KICKOFF_B = datetime(2025, 10, 19, 20, 0, tzinfo=UTC)
GAME_A, GAME_B = "2025_08_KC_LV", "2025_08_GB_CHI"
EVENT_A, EVENT_B = "ev-a", "ev-b"

# (game id, home, away, kickoff, event id, home team name, away team name)
GAMES = [
    (GAME_A, "LV", "KC", KICKOFF_A, EVENT_A, "Las Vegas Raiders", "Kansas City Chiefs"),
    (GAME_B, "CHI", "GB", KICKOFF_B, EVENT_B, "Chicago Bears", "Green Bay Packers"),
]
# (id, name, team, position)
PLAYERS = [
    (1, "Isiah Pacheco", "KC", "RB"),
    (2, "Travis Kelce", "KC", "TE"),
    (3, "Jordan Love", "GB", "QB"),
    (4, "D'Andre Swift", "CHI", "RB"),
]
# The two players each event's quotes are about: (rushing, touchdown).
EVENT_PLAYERS = {
    EVENT_A: ("Isiah Pacheco", "Travis Kelce"),
    EVENT_B: ("Jordan Love", "D'Andre Swift"),
}


def build_world(session: Session, *, linked: bool = True) -> Session:
    seed(session)
    for game_id, home, away, kickoff, event, *_ in GAMES:
        session.add(
            Game(
                id=game_id,
                season=2025,
                week=8,
                season_type="REG",
                kickoff_utc=kickoff,
                home_abbr=home,
                away_abbr=away,
                odds_event_id=event if linked else None,
            )
        )
    for player_id, name, team, position in PLAYERS:
        session.add(Player(id=player_id, full_name=name, position=position))
        session.flush()
        session.add(
            Roster(
                season=2025,
                week=8,
                player_id=player_id,
                team_abbr=team,
                position=position,
                available_at=KICKOFF_A - timedelta(days=5),
            )
        )
    session.commit()
    return session


def make_config(**changes: Any) -> BackfillConfig:
    base = BackfillConfig(
        source="the_odds_api",
        markets=MARKETS,
        regions="us",
        fuzzy_threshold=90,
        stale_after=timedelta(minutes=90),
        max_rps=1000,
        reserve=0,
    )
    return BackfillConfig(**{**base.__dict__, **changes})


@dataclass
class Call:
    event_id: str
    at: datetime
    snapshot_at: datetime
    quotes: int
    charged: int


@dataclass
class FakeHistory:
    """Historical snapshots on a fixed grid. Props exist from `posted[event]` until
    `pulled[event]`; a call is charged 10 credits per market it returns, and a call with
    nothing in it is free. The budget check is up front, like the real adapter's."""

    posted: dict[str, datetime | None]
    pulled: dict[str, datetime] = field(default_factory=dict)
    interval: timedelta = timedelta(minutes=5)
    budget: int = 10**9
    plan_remaining: int | None = None
    price: Callable[[str, datetime], int] = lambda event, snapshot: -110
    errors: dict[str, str] = field(default_factory=dict)
    quotes_for: dict[str, list[str]] = field(default_factory=dict)
    events: list[OddsEvent] = field(default_factory=list)
    narrow_only: set[str] = field(default_factory=set)
    calls: list[Call] = field(default_factory=list)
    event_calls: list[tuple[datetime, datetime, datetime, int]] = field(default_factory=list)
    used: int = 0

    # Historical props ------------------------------------------------------------------------

    def snapshot_for(self, at: datetime) -> datetime:
        step = self.interval.total_seconds()
        return datetime.fromtimestamp((at.timestamp() // step) * step, UTC)

    def get_historical_event_props(
        self,
        event_id: str,
        at: datetime,
        markets: Sequence[str],
        bookmakers: Sequence[str] | None = None,
    ) -> PropsResponse:
        cost = 10 * len(markets)
        if self.used + cost > self.budget:
            raise CreditBudgetExceeded(f"{event_id} could cost {cost}")
        if self.plan_remaining is not None and cost > self.plan_remaining - self.used:
            raise CreditBudgetExceeded("plan credits")
        if event_id in self.errors:
            raise OddsProviderError(self.errors[event_id])

        snapshot = self.snapshot_for(at)
        posted = self.posted.get(event_id)
        live = posted is not None and posted <= snapshot < self.pulled.get(
            event_id, datetime.max.replace(tzinfo=UTC)
        )
        quotes = self._quotes(event_id, snapshot, markets) if live else ()
        charged = 10 * len({q.market for q in quotes})
        self.used += charged
        self.calls.append(Call(event_id, at, snapshot, len(quotes), charged))
        return PropsResponse(
            quotes=tuple(quotes),
            snapshot_at=snapshot,
            previous_snapshot_at=snapshot - self.interval,
            next_snapshot_at=snapshot + self.interval,
        )

    def _quotes(self, event_id: str, snapshot: datetime, markets: Sequence[str]) -> list[PropQuote]:
        rusher, scorer = self.quotes_for.get(event_id) or EVENT_PLAYERS[event_id]
        price = self.price(event_id, snapshot)

        def quote(market: str, name: str, side: str, line: float | None, p: int) -> PropQuote:
            return PropQuote(
                event_id=event_id,
                book="draftkings",
                market=market,
                player_name_raw=name,
                side=side,  # type: ignore[arg-type]
                line=line,
                price=p,
                captured_at=snapshot,
            )

        quotes: list[PropQuote] = []
        if "player_rush_yds" in markets:
            quotes += [
                quote("player_rush_yds", rusher, "over", 64.5, price),
                quote("player_rush_yds", rusher, "under", 64.5, -105),
            ]
        if "player_anytime_td" in markets:
            quotes += [
                quote("player_anytime_td", scorer, "yes", None, 120),
                quote("player_anytime_td", scorer, "no", None, -150),
            ]
        return quotes

    # Historical events -----------------------------------------------------------------------

    def list_historical_events(
        self, at: datetime, commence_from: datetime, commence_to: datetime
    ) -> list[OddsEvent]:
        narrow = commence_to - commence_from < timedelta(hours=6)
        found = [
            e
            for e in self.events
            if commence_from <= e.commence_time <= commence_to
            and (narrow or e.event_id not in self.narrow_only)
        ]
        cost = 1 if found else 0
        if self.used + cost > self.budget:
            raise CreditBudgetExceeded("events")
        self.used += cost
        self.event_calls.append((at, commence_from, commence_to, cost))
        return found

    # The rest of the provider ----------------------------------------------------------------

    def list_events(self, *args: Any, **kwargs: Any) -> list[OddsEvent]:
        raise AssertionError("live endpoint")

    def list_event_markets(self, event_id: str) -> list[EventMarkets]:
        raise AssertionError("live endpoint")

    def get_event_props(self, *args: Any, **kwargs: Any) -> PropsResponse:
        raise AssertionError("live endpoint")

    def credit_usage(self) -> CreditUsage:
        remaining = None if self.plan_remaining is None else self.plan_remaining - self.used
        return CreditUsage(
            used=None,
            remaining=remaining,
            last_call=None,
            used_today=self.used,
            daily_budget=self.budget,
        )

    @property
    def paid_calls(self) -> int:
        return sum(1 for c in self.calls if c.charged)


def events_for_games() -> list[OddsEvent]:
    return [
        OddsEvent(event, home_name, away_name, kickoff)
        for _, _, _, kickoff, event, home_name, away_name in GAMES
    ]
