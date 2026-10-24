import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ingest.odds.markets import FANTASY_MARKETS
from ingest.odds.provider import CreditBudgetExceeded, OddsProviderError
from ingest.odds.the_odds_api import TheOddsApiProvider

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "odds_api"
API_KEY = "test-key-do-not-log"
NOW = datetime(2026, 10, 24, 15, 5, tzinfo=UTC)
EVENT_ID = "e912304de2b2ce35b473ce2ecd3d1502"

Handler = Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def ok(body: Any, *, last: int = 0, remaining: int = 480, used: int = 20) -> httpx.Response:
    headers = {
        "x-requests-last": str(last),
        "x-requests-remaining": str(remaining),
        "x-requests-used": str(used),
    }
    return httpx.Response(200, json=body, headers=headers)


class Recorder:
    """Mock transport that records requests and replays queued responses."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def provider(
    recorder: Recorder,
    *,
    budget: int = 15,
    used_today: int = 0,
    clock: Callable[[], datetime] = lambda: NOW,
    sleeps: list[float] | None = None,
) -> TheOddsApiProvider:
    return TheOddsApiProvider(
        api_key=API_KEY,
        daily_credit_budget=budget,
        used_today=used_today,
        transport=httpx.MockTransport(recorder),
        clock=clock,
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
    )


# Parsing ---------------------------------------------------------------------------------------


def test_list_events() -> None:
    recorder = Recorder(ok(fixture("events.json")))
    events = provider(recorder).list_events(NOW, NOW + timedelta(days=4))

    assert [e.event_id for e in events] == [EVENT_ID, "a512a48a58c4329048174217b2cc7ce0"]
    assert events[0].home_team == "Las Vegas Raiders"
    assert events[0].commence_time == datetime(2026, 10, 25, 20, 25, tzinfo=UTC)
    request = recorder.requests[0]
    assert request.url.path == "/v4/sports/americanfootball_nfl/events"
    assert request.url.params["commenceTimeFrom"] == "2026-10-24T15:05:00Z"


def test_list_event_markets() -> None:
    recorder = Recorder(ok(fixture("event_markets.json"), last=1))
    markets = provider(recorder).list_event_markets(EVENT_ID)

    assert {m.book: m.markets for m in markets} == {
        "draftkings": ("player_pass_yds", "player_rush_yds", "player_anytime_td"),
        "fanduel": ("player_rush_yds", "player_rush_yds_alternate"),
    }


def test_event_props_are_parsed_into_quotes() -> None:
    recorder = Recorder(ok(fixture("event_odds.json"), last=3))
    response = provider(recorder).get_event_props(
        EVENT_ID, ["player_rush_yds", "player_anytime_td", "player_rush_yds_alternate"]
    )
    quotes = response.quotes

    assert response.snapshot_at == NOW
    # 4 + 3 at DraftKings, 2 + 3 at FanDuel; the outcome with no player name is skipped.
    assert len(quotes) == 12
    assert all(q.event_id == EVENT_ID and q.captured_at == NOW for q in quotes)

    pacheco_dk = [
        q
        for q in quotes
        if q.book == "draftkings"
        and q.market == "player_rush_yds"
        and q.player_name_raw == "Isiah Pacheco"
    ]
    assert {(q.side, q.line, q.price) for q in pacheco_dk} == {
        ("over", 64.5, -115),
        ("under", 64.5, -105),
    }

    td = [q for q in quotes if q.market == "player_anytime_td"]
    assert {(q.player_name_raw, q.side) for q in td} == {
        ("Isiah Pacheco", "yes"),
        ("Isiah Pacheco", "no"),
        ("Travis Kelce", "yes"),
    }
    assert all(q.line is None for q in td)

    alternate_lines = sorted(
        q.line for q in quotes if q.market == "player_rush_yds_alternate" and q.line is not None
    )
    assert alternate_lines == [49.5, 74.5, 99.5]


def test_event_props_request_parameters() -> None:
    recorder = Recorder(ok(fixture("event_odds.json"), last=2))
    provider(recorder).get_event_props(EVENT_ID, ["player_rush_yds", "player_anytime_td"])
    params = recorder.requests[0].url.params

    assert recorder.requests[0].url.path == (
        f"/v4/sports/americanfootball_nfl/events/{EVENT_ID}/odds"
    )
    assert params["markets"] == "player_rush_yds,player_anytime_td"
    assert params["oddsFormat"] == "american"
    assert params["regions"] == "us"
    assert "bookmakers" not in params
    assert params["apiKey"] == API_KEY


def test_bookmakers_replace_regions() -> None:
    recorder = Recorder(ok(fixture("event_odds.json"), last=1))
    provider(recorder).get_event_props(EVENT_ID, ["player_rush_yds"], ["draftkings", "fanduel"])
    params = recorder.requests[0].url.params

    assert params["bookmakers"] == "draftkings,fanduel"
    assert "regions" not in params


def test_historical_props_use_the_snapshot_timestamp() -> None:
    recorder = Recorder(ok(fixture("historical_event_odds.json"), last=10))
    at = datetime(2025, 10, 19, 16, 57, tzinfo=UTC)
    response = provider(recorder, budget=50).get_historical_event_props(
        "3fa5d4c2b8a8a1e5f8e2d0c4b6a71234", at, ["player_reception_yds"]
    )

    assert response.snapshot_at == datetime(2025, 10, 19, 16, 55, tzinfo=UTC)
    assert {(q.side, q.line, q.price) for q in response.quotes} == {
        ("over", 52.5, -118),
        ("under", 52.5, -110),
    }
    assert all(q.captured_at == response.snapshot_at for q in response.quotes)
    request = recorder.requests[0]
    assert request.url.path.startswith("/v4/historical/sports/americanfootball_nfl/events/")
    assert request.url.params["date"] == "2025-10-19T16:57:00Z"


def test_seeded_markets_are_all_documented_provider_keys() -> None:
    documented = set(fixture("documented_nfl_prop_markets.json")["markets"])
    assert set(FANTASY_MARKETS) <= documented


# Credit budget ---------------------------------------------------------------------------------


def test_call_over_the_daily_budget_is_refused_without_a_request() -> None:
    recorder = Recorder()
    odds = provider(recorder, budget=15, used_today=10)

    with pytest.raises(CreditBudgetExceeded, match="up to 9 credits"):
        odds.get_event_props(EVENT_ID, list(FANTASY_MARKETS[:9]))
    assert recorder.requests == []


def test_historical_calls_are_budgeted_at_ten_times() -> None:
    recorder = Recorder()
    with pytest.raises(CreditBudgetExceeded, match="up to 20 credits"):
        provider(recorder, budget=15).get_historical_event_props(
            EVENT_ID, NOW, ["player_rush_yds", "player_reception_yds"]
        )
    assert recorder.requests == []


def test_free_endpoints_are_allowed_with_the_budget_spent() -> None:
    recorder = Recorder(ok(fixture("events.json")))
    assert provider(recorder, budget=15, used_today=15).list_events(NOW, NOW)


def test_usage_comes_from_response_headers() -> None:
    recorder = Recorder(ok(fixture("event_odds.json"), last=3, remaining=477, used=23))
    odds = provider(recorder)
    odds.get_event_props(EVENT_ID, ["player_rush_yds", "player_anytime_td", "player_receptions"])
    usage = odds.credit_usage()

    assert (usage.last_call, usage.remaining, usage.used, usage.used_today) == (3, 477, 23, 3)


def test_plan_remaining_credits_are_respected() -> None:
    recorder = Recorder(ok(fixture("event_odds.json"), last=1, remaining=2))
    odds = provider(recorder, budget=100)
    odds.get_event_props(EVENT_ID, ["player_rush_yds"])

    with pytest.raises(CreditBudgetExceeded, match="only 2 remain"):
        odds.get_event_props(
            EVENT_ID, ["player_rush_yds", "player_anytime_td", "player_receptions"]
        )


def test_daily_counter_resets_at_utc_midnight() -> None:
    now = [NOW]
    recorder = Recorder(ok(fixture("event_odds.json"), last=5))
    odds = provider(recorder, budget=15, used_today=12, clock=lambda: now[0])

    now[0] = NOW + timedelta(days=1)
    odds.get_event_props(EVENT_ID, list(FANTASY_MARKETS[:5]))
    assert odds.credit_usage().used_today == 5


# Retries and errors ----------------------------------------------------------------------------


def test_rate_limit_is_retried_with_backoff() -> None:
    sleeps: list[float] = []
    recorder = Recorder(httpx.Response(429), httpx.Response(429), ok(fixture("events.json")))
    events = provider(recorder, sleeps=sleeps).list_events(NOW, NOW)

    assert len(events) == 2
    assert sleeps == [1.0, 2.0]


def test_retry_after_header_is_honoured() -> None:
    sleeps: list[float] = []
    recorder = Recorder(
        httpx.Response(429, headers={"retry-after": "7"}), ok(fixture("events.json"))
    )
    provider(recorder, sleeps=sleeps).list_events(NOW, NOW)
    assert sleeps == [7.0]


def test_timeouts_are_retried() -> None:
    recorder = Recorder(httpx.ReadTimeout("slow"), ok(fixture("events.json")))
    assert provider(recorder).list_events(NOW, NOW)
    assert len(recorder.requests) == 2


def test_server_errors_give_up_after_max_attempts() -> None:
    recorder = Recorder(*(httpx.Response(503) for _ in range(4)))
    with pytest.raises(OddsProviderError, match="HTTP 503"):
        provider(recorder).list_events(NOW, NOW)
    assert len(recorder.requests) == 4


def test_client_errors_are_not_retried_and_hide_the_key() -> None:
    recorder = Recorder(
        httpx.Response(401, json={"message": "API key is not valid", "error_code": "INVALID_KEY"})
    )
    with pytest.raises(OddsProviderError, match="API key is not valid") as excinfo:
        provider(recorder).list_events(NOW, NOW)

    assert len(recorder.requests) == 1
    assert API_KEY not in str(excinfo.value)


def test_logs_never_contain_the_api_key(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    recorder = Recorder(httpx.Response(500), ok(fixture("event_odds.json"), last=1))
    provider(recorder).get_event_props(EVENT_ID, ["player_rush_yds"])

    assert caplog.records
    assert API_KEY not in caplog.text
    for record in caplog.records:
        assert API_KEY not in json.dumps(vars(record), default=str)


def test_missing_api_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="ODDS_API_KEY"):
        TheOddsApiProvider(api_key="", daily_credit_budget=15)
