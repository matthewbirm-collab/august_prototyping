"""A provider for code paths that must not touch the network (dry runs, estimates)."""

from collections.abc import Sequence
from datetime import datetime

from ingest.odds.provider import CreditUsage, EventMarkets, OddsEvent, PropsResponse


class OfflineProvider:
    """Stands in for a provider when only the database is needed; any API call is a bug."""

    def list_events(self, commence_from: datetime, commence_to: datetime) -> list[OddsEvent]:
        raise RuntimeError("this operation makes no API calls")

    def list_event_markets(self, event_id: str) -> list[EventMarkets]:
        raise RuntimeError("this operation makes no API calls")

    def get_event_props(
        self, event_id: str, markets: Sequence[str], bookmakers: Sequence[str] | None = None
    ) -> PropsResponse:
        raise RuntimeError("this operation makes no API calls")

    def get_historical_event_props(
        self,
        event_id: str,
        at: datetime,
        markets: Sequence[str],
        bookmakers: Sequence[str] | None = None,
    ) -> PropsResponse:
        raise RuntimeError("this operation makes no API calls")

    def list_historical_events(
        self, at: datetime, commence_from: datetime, commence_to: datetime
    ) -> list[OddsEvent]:
        raise RuntimeError("this operation makes no API calls")

    def credit_usage(self) -> CreditUsage:
        raise RuntimeError("this operation makes no API calls")
