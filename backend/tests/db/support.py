"""Shared fixtures data and fakes for the capture and run-ingest tests."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.settings import DEFAULT_POLL_TIERS
from ingest.capture import CaptureConfig, OddsCapture
from ingest.odds.markets import MAIN_MARKETS
from ingest.odds.provider import (
    CreditBudgetExceeded,
    CreditUsage,
    EventMarkets,
    OddsEvent,
    PropQuote,
    PropsResponse,
)

NOW = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)

# (game id, week, home, away, kickoff offset from NOW, odds event id)
GAMES = [
    ("2026_08_KC_LV", 8, "LV", "KC", timedelta(hours=2), "ev-soon"),  # 15-minute tier
    ("2026_08_GB_CHI", 8, "CHI", "GB", timedelta(hours=20), "ev-day"),  # hourly
    ("2026_08_BUF_MIA", 8, "MIA", "BUF", timedelta(days=5), "ev-far"),  # every 12h
    ("2026_08_DAL_PHI", 8, "PHI", "DAL", timedelta(hours=26), None),  # not linked yet
    ("2026_07_SEA_SF", 7, "SF", "SEA", -timedelta(hours=1), "ev-past"),  # already kicked off
    ("2026_09_NYJ_NE", 9, "NE", "NYJ", timedelta(days=9), "ev-beyond"),  # past the horizon
]
SOON, DAY, FAR = "2026_08_KC_LV", "2026_08_GB_CHI", "2026_08_BUF_MIA"

# (player id, name, team, position)
PLAYERS = [
    (1, "Isiah Pacheco", "KC", "RB"),
    (2, "Travis Kelce", "KC", "TE"),
    (3, "Zamir White", "LV", "RB"),
    (4, "Jordan Love", "GB", "QB"),
    (5, "Josh Allen", "BUF", "QB"),
]


def quote(
    event: str,
    market: str,
    name: str,
    side: str,
    line: float | None,
    price: int,
    book: str = "draftkings",
) -> PropQuote:
    return PropQuote(
        event_id=event,
        book=book,
        market=market,
        player_name_raw=name,
        side=side,  # type: ignore[arg-type]
        line=line,
        price=price,
        captured_at=NOW,
    )


def soon_props(event: str = "ev-soon") -> list[PropQuote]:
    return [
        quote(event, "player_rush_yds", "Isiah Pacheco", "over", 64.5, -115),
        quote(event, "player_rush_yds", "Isiah Pacheco", "under", 64.5, -105),
        quote(event, "player_anytime_td", "Travis Kelce", "yes", None, 120),
        quote(event, "player_anytime_td", "Travis Kelce", "no", None, -150),
    ]


class FakeOdds:
    """An OddsProvider with scripted responses and a credit counter of its own."""

    def __init__(
        self,
        responses: dict[str, list[PropQuote] | Exception],
        *,
        events: Sequence[OddsEvent] = (),
        budget: int = 100,
        used_today: int = 0,
    ) -> None:
        self.responses = responses
        self.events = list(events)
        self.budget = budget
        self.used = used_today
        self.polled: list[str] = []

    def list_events(self, commence_from: datetime, commence_to: datetime) -> list[OddsEvent]:
        return [e for e in self.events if commence_from <= e.commence_time <= commence_to]

    def list_event_markets(self, event_id: str) -> list[EventMarkets]:
        return []

    def get_event_props(
        self, event_id: str, markets: Sequence[str], bookmakers: Sequence[str] | None = None
    ) -> PropsResponse:
        cost = len(markets)
        if self.used + cost > self.budget:
            raise CreditBudgetExceeded(f"{event_id} needs {cost}")
        self.used += cost
        self.polled.append(event_id)
        result = self.responses[event_id]
        if isinstance(result, Exception):
            raise result
        return PropsResponse(quotes=tuple(result), snapshot_at=NOW)

    def get_historical_event_props(self, *args: Any, **kwargs: Any) -> PropsResponse:
        raise AssertionError("not used")

    def credit_usage(self) -> CreditUsage:
        return CreditUsage(
            used=None,
            remaining=None,
            last_call=None,
            used_today=self.used,
            daily_budget=self.budget,
        )


def make_config(**changes: Any) -> CaptureConfig:
    base = CaptureConfig(
        source="the_odds_api",
        markets=tuple(MAIN_MARKETS),
        regions="us",
        tiers=tuple(DEFAULT_POLL_TIERS),
        fuzzy_threshold=90,
        horizon_days=7,
    )
    return CaptureConfig(**{**base.__dict__, **changes})


def make_capture(
    db: Session, provider: Any, hook: Any = None, **config_changes: Any
) -> OddsCapture:
    extra = {"on_new_snapshots": hook} if hook is not None else {}
    return OddsCapture(db, provider, make_config(**config_changes), clock=lambda: NOW, **extra)


def standard_responses() -> dict[str, list[PropQuote] | Exception]:
    return {
        "ev-soon": soon_props("ev-soon"),
        "ev-day": [quote("ev-day", "player_pass_yds", "Jordan Love", "over", 245.5, -110)],
        "ev-far": [quote("ev-far", "player_pass_yds", "Josh Allen", "over", 251.5, -112)],
    }
