"""The Odds API (v4) implementation of `OddsProvider`.

Docs: https://the-odds-api.com/liveapi/guides/v4/. Player props come only from the per-event
odds endpoint, one game per call. Costs, from the docs:

- events list: free
- event markets: 1 credit
- event odds: unique markets returned x regions
- historical event odds: 10 x unique markets returned x regions (paid plans only)

Every 10 bookmakers passed in `bookmakers` count as one region. Empty responses are free.
"""

import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, cast

import httpx

from ingest.odds.provider import (
    CreditBudgetExceeded,
    CreditUsage,
    EventMarkets,
    OddsEvent,
    OddsProviderError,
    PropQuote,
    PropsResponse,
    Side,
)

logger = logging.getLogger(__name__)
# httpx logs full request URLs at INFO, and the API key travels as a query parameter.
logging.getLogger("httpx").setLevel(logging.WARNING)

SPORT = "americanfootball_nfl"
HISTORICAL_MULTIPLIER = 10
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
SIDES: Mapping[str, Side] = {"over": "over", "under": "under", "yes": "yes", "no": "no"}


class TheOddsApiProvider:
    def __init__(
        self,
        api_key: str,
        daily_credit_budget: int,
        *,
        regions: str = "us",
        base_url: str = "https://api.the-odds-api.com",
        timeout_seconds: float = 10.0,
        max_attempts: int = 4,
        backoff_seconds: float = 1.0,
        used_today: int = 0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """`used_today` seeds the daily counter, e.g. from today's `ingestion_runs`."""
        if not api_key:
            raise ValueError("The Odds API needs an API key (ODDS_API_KEY)")
        self._api_key = api_key
        self._budget = daily_credit_budget
        self._regions = regions
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._clock = clock
        self._sleep = sleep
        self._client = httpx.Client(base_url=base_url, timeout=timeout_seconds, transport=transport)
        self._day: date = clock().date()
        self._used_today = used_today
        self._used: int | None = None
        self._remaining: int | None = None
        self._last: int | None = None

    def close(self) -> None:
        self._client.close()

    # OddsProvider ----------------------------------------------------------------------------

    def list_events(self, commence_from: datetime, commence_to: datetime) -> list[OddsEvent]:
        data = self._get(
            f"/v4/sports/{SPORT}/events",
            {"commenceTimeFrom": _iso(commence_from), "commenceTimeTo": _iso(commence_to)},
            max_cost=0,
        )
        return [
            OddsEvent(
                event_id=event["id"],
                home_team=event["home_team"],
                away_team=event["away_team"],
                commence_time=_parse_time(event["commence_time"]),
            )
            for event in cast(list[dict[str, Any]], data)
        ]

    def list_event_markets(self, event_id: str) -> list[EventMarkets]:
        data = self._get(
            f"/v4/sports/{SPORT}/events/{event_id}/markets",
            {"regions": self._regions},
            max_cost=1,
        )
        return [
            EventMarkets(
                book=book["key"], markets=tuple(market["key"] for market in book["markets"])
            )
            for book in cast(dict[str, Any], data).get("bookmakers", [])
        ]

    def get_event_props(
        self,
        event_id: str,
        markets: Sequence[str],
        bookmakers: Sequence[str] | None = None,
    ) -> PropsResponse:
        params = self._odds_params(markets, bookmakers)
        data = self._get(
            f"/v4/sports/{SPORT}/events/{event_id}/odds",
            params,
            max_cost=len(markets) * self._region_count(bookmakers),
        )
        snapshot_at = self._clock()
        return PropsResponse(
            quotes=tuple(_parse_quotes(cast(dict[str, Any], data), snapshot_at)),
            snapshot_at=snapshot_at,
        )

    def get_historical_event_props(
        self,
        event_id: str,
        at: datetime,
        markets: Sequence[str],
        bookmakers: Sequence[str] | None = None,
    ) -> PropsResponse:
        params = {**self._odds_params(markets, bookmakers), "date": _iso(at)}
        wrapper = cast(
            dict[str, Any],
            self._get(
                f"/v4/historical/sports/{SPORT}/events/{event_id}/odds",
                params,
                max_cost=HISTORICAL_MULTIPLIER * len(markets) * self._region_count(bookmakers),
            ),
        )
        snapshot_at = _parse_time(wrapper["timestamp"])
        return PropsResponse(
            quotes=tuple(_parse_quotes(wrapper.get("data") or {}, snapshot_at)),
            snapshot_at=snapshot_at,
        )

    def credit_usage(self) -> CreditUsage:
        self._roll_day()
        return CreditUsage(
            used=self._used,
            remaining=self._remaining,
            last_call=self._last,
            used_today=self._used_today,
            daily_budget=self._budget,
        )

    # Internals -------------------------------------------------------------------------------

    def _odds_params(
        self, markets: Sequence[str], bookmakers: Sequence[str] | None
    ) -> dict[str, str]:
        if not markets:
            raise ValueError("at least one market is required")
        params = {"markets": ",".join(markets), "oddsFormat": "american", "dateFormat": "iso"}
        if bookmakers:
            params["bookmakers"] = ",".join(bookmakers)
        else:
            params["regions"] = self._regions
        return params

    def _region_count(self, bookmakers: Sequence[str] | None) -> int:
        if bookmakers:
            return math.ceil(len(bookmakers) / 10)
        return len(self._regions.split(","))

    def _roll_day(self) -> None:
        today = self._clock().date()
        if today != self._day:
            self._day = today
            self._used_today = 0

    def _check_budget(self, path: str, max_cost: int) -> None:
        self._roll_day()
        if self._used_today + max_cost > self._budget:
            raise CreditBudgetExceeded(
                f"{path} could cost up to {max_cost} credits; {self._used_today} of the "
                f"{self._budget} daily budget already used"
            )
        if self._remaining is not None and max_cost > self._remaining:
            raise CreditBudgetExceeded(
                f"{path} could cost up to {max_cost} credits; only {self._remaining} remain on "
                "the plan"
            )

    def _get(self, path: str, params: dict[str, str], *, max_cost: int) -> Any:
        """GET with budget enforcement and retries. Never logs or raises the API key."""
        self._check_budget(path, max_cost)
        query = {**params, "apiKey": self._api_key}

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.get(path, params=query)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == self._max_attempts:
                    raise OddsProviderError(f"{path}: {type(exc).__name__} after retries") from None
                self._wait(path, attempt, reason=type(exc).__name__)
                continue

            if response.status_code in RETRY_STATUSES and attempt < self._max_attempts:
                self._wait(path, attempt, reason=str(response.status_code), response=response)
                continue
            if response.is_error:
                raise OddsProviderError(
                    f"{path}: HTTP {response.status_code}: {_error_message(response)}"
                )

            cost = self._record_usage(response, max_cost)
            logger.info(
                "odds api call",
                extra={
                    "path": path,
                    "status": response.status_code,
                    "attempt": attempt,
                    "credits_last": cost,
                    "credits_remaining": self._remaining,
                    "credits_used_today": self._used_today,
                },
            )
            return response.json()

        raise AssertionError("unreachable")  # pragma: no cover

    def _wait(
        self, path: str, attempt: int, *, reason: str, response: httpx.Response | None = None
    ) -> None:
        delay = min(self._backoff * 2 ** (attempt - 1), 30.0)
        retry_after = response.headers.get("retry-after") if response is not None else None
        if retry_after and retry_after.isdigit():
            delay = max(delay, float(retry_after))
        logger.warning(
            "odds api retry",
            extra={"path": path, "attempt": attempt, "reason": reason, "delay_seconds": delay},
        )
        self._sleep(delay)

    def _record_usage(self, response: httpx.Response, estimate: int) -> int:
        self._used = _int_header(response, "x-requests-used", self._used)
        self._remaining = _int_header(response, "x-requests-remaining", self._remaining)
        last = _int_header(response, "x-requests-last", None)
        cost = estimate if last is None else last
        self._last = cost
        self._used_today += cost
        return cost


def _parse_quotes(event: dict[str, Any], captured_at: datetime) -> list[PropQuote]:
    quotes: list[PropQuote] = []
    event_id = event.get("id", "")
    for book in event.get("bookmakers", []):
        for market in book.get("markets", []):
            for outcome in market.get("outcomes", []):
                side = SIDES.get(str(outcome.get("name", "")).lower())
                player = outcome.get("description")
                price = outcome.get("price")
                if side is None or not player or price is None:
                    logger.warning(
                        "skipped unparseable outcome",
                        extra={"event_id": event_id, "book": book.get("key"), "outcome": outcome},
                    )
                    continue
                point = outcome.get("point")
                quotes.append(
                    PropQuote(
                        event_id=event_id,
                        book=book["key"],
                        market=market["key"],
                        player_name_raw=player,
                        side=side,
                        line=None if point is None else float(point),
                        price=round(price),
                        captured_at=captured_at,
                    )
                )
    return quotes


def _int_header(response: httpx.Response, name: str, default: int | None) -> int | None:
    value = response.headers.get(name)
    try:
        return int(float(value)) if value is not None else default
    except ValueError:
        return default


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict) and "message" in body:
        return str(body["message"])
    return str(body)[:200]


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
