import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (
    Book,
    Game,
    IngestionRun,
    PlayerAlias,
    PropSnapshot,
    UnmatchedName,
)
from ingest.capture import (
    credits_used_today,
    has_previous_runs,
)
from ingest.odds.markets import MAIN_MARKETS
from ingest.odds.provider import (
    OddsEvent,
    OddsProviderError,
)
from ingest.odds.the_odds_api import TheOddsApiProvider
from ingest.recompute import RecomputeRequest
from tests.db.support import (
    DAY,
    FAR,
    NOW,
    SOON,
    FakeOdds,
    make_capture,
    quote,
    soon_props,
    standard_responses,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "odds_api"


@pytest.fixture
def db(capture_db: Session) -> Session:
    return capture_db


def snapshot_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(PropSnapshot)) or 0


def record_poll(db: Session, game_id: str, minutes_ago: float, status: str = "ok") -> None:
    polled = NOW - timedelta(minutes=minutes_ago)
    db.add(
        IngestionRun(
            kind="odds_live",
            started_at=polled,
            status=status,
            details={"games": {game_id: {"polled_at": polled.isoformat()}}},
        )
    )
    db.commit()


# Choosing games --------------------------------------------------------------------------------


def test_due_games_are_in_the_horizon_linked_and_not_kicked_off(db: Session) -> None:
    due, unlinked = make_capture(db, FakeOdds({})).due_games(NOW)
    assert [g.id for g in due] == [SOON, DAY, FAR]  # soonest first
    assert unlinked == 1


def test_recently_polled_games_are_not_due(db: Session) -> None:
    record_poll(db, SOON, minutes_ago=5)  # 15-minute tier
    record_poll(db, DAY, minutes_ago=50)  # hourly tier
    record_poll(db, FAR, minutes_ago=11 * 60)  # 12-hour tier
    due, _ = make_capture(db, FakeOdds({})).due_games(NOW)
    assert due == []


def test_games_past_their_interval_are_due_again(db: Session) -> None:
    record_poll(db, SOON, minutes_ago=14)
    record_poll(db, DAY, minutes_ago=59)
    record_poll(db, FAR, minutes_ago=12 * 60)
    due, _ = make_capture(db, FakeOdds({})).due_games(NOW)
    assert [g.id for g in due] == [SOON, DAY, FAR]


def test_failed_runs_do_not_count_as_polls(db: Session) -> None:
    record_poll(db, SOON, minutes_ago=2, status="failed")
    due, _ = make_capture(db, FakeOdds({})).due_games(NOW)
    assert SOON in [g.id for g in due]


def test_forced_tier_polls_only_games_in_that_tier(db: Session) -> None:
    capture = make_capture(db, FakeOdds({}))
    assert [g.id for g in capture.due_games(NOW, "15m")[0]] == [SOON]
    assert [g.id for g in capture.due_games(NOW, "1h")[0]] == [DAY]
    assert [g.id for g in capture.due_games(NOW, "12h")[0]] == [FAR]
    with pytest.raises(ValueError, match="no polling tier '6h'"):
        capture.due_games(NOW, "6h")


# Running ---------------------------------------------------------------------------------------


def test_run_stores_quotes_and_records_the_run(db: Session) -> None:
    odds = FakeOdds(standard_responses())
    summary = make_capture(db, odds).run()

    assert summary.status == "ok"
    assert odds.polled == ["ev-soon", "ev-day", "ev-far"]
    assert (summary.games_polled, summary.quotes, summary.new_snapshots) == (3, 6, 4)
    assert summary.credits_used == 3 * len(MAIN_MARKETS)

    rows = {(s.game_id, s.market_key, s.player_id): s for s in db.scalars(select(PropSnapshot))}
    rush = rows[(SOON, "player_rush_yds", 1)]
    assert (rush.line, rush.over_price, rush.under_price) == (64.5, -115, -105)
    assert (rush.source, rush.book_key, rush.captured_at) == ("live", "draftkings", NOW)
    td = rows[(SOON, "player_anytime_td", 2)]
    assert (td.line, td.over_price, td.under_price) == (None, 120, -150)

    run = db.scalars(select(IngestionRun)).one()
    assert (run.kind, run.status, run.events, run.quotes, run.new_snapshots) == (
        "odds_live",
        "ok",
        3,
        6,
        4,
    )
    assert run.credits_used == 27
    assert run.finished_at is not None
    assert run.errors == []
    assert set(run.details["games"]) == {SOON, DAY, FAR}
    assert all(row.ingestion_run_id == run.id for row in rows.values())


def test_a_second_run_right_after_has_nothing_due(db: Session) -> None:
    odds = FakeOdds(standard_responses())
    capture = make_capture(db, odds)
    capture.run()
    again = capture.run()

    assert again.status == "idle"
    assert again.run_id is None
    assert db.scalar(select(func.count()).select_from(IngestionRun)) == 1
    assert odds.polled == ["ev-soon", "ev-day", "ev-far"]  # no second round of calls


def test_unchanged_quotes_are_not_stored_twice(db: Session) -> None:
    capture = make_capture(db, FakeOdds(standard_responses()))
    first = capture.run("15m")
    second = capture.run("15m")

    assert (first.new_snapshots, second.new_snapshots) == (2, 0)
    assert second.status == "ok"
    assert second.quotes == 4  # polled and recorded, but nothing new to store
    assert snapshot_count(db) == 2


def test_a_price_change_is_stored_as_a_new_snapshot(db: Session) -> None:
    responses = standard_responses()
    capture = make_capture(db, FakeOdds(responses))
    capture.run("15m")

    responses["ev-soon"] = [
        quote("ev-soon", "player_rush_yds", "Isiah Pacheco", "over", 64.5, -125),
        quote("ev-soon", "player_rush_yds", "Isiah Pacheco", "under", 64.5, 105),
    ]
    assert capture.run("15m").new_snapshots == 1
    assert snapshot_count(db) == 3


def test_one_game_failing_does_not_stop_the_others(db: Session) -> None:
    responses = standard_responses()
    responses["ev-day"] = OddsProviderError("HTTP 503: upstream down")
    summary = make_capture(db, FakeOdds(responses)).run()

    assert summary.status == "partial"
    assert summary.games_polled == 2
    assert [e["game"] for e in summary.errors] == [DAY]
    assert "HTTP 503" in summary.errors[0]["error"]
    assert {s.game_id for s in db.scalars(select(PropSnapshot))} == {SOON, FAR}

    run = db.scalars(select(IngestionRun)).one()
    assert run.status == "partial"
    assert run.errors == summary.errors
    # The failed game isn't recorded as polled, so the next run retries it.
    assert set(run.details["games"]) == {SOON, FAR}


def test_a_storage_error_rolls_back_only_that_game(db: Session) -> None:
    responses = standard_responses()
    # Pacheco's rush line is fine, but a missing price makes the row invalid.
    responses["ev-soon"] = [
        quote("ev-soon", "player_rush_yds", "Isiah Pacheco", "over", 64.5, 50),  # not American odds
    ]
    summary = make_capture(db, FakeOdds(responses)).run()

    assert summary.status == "partial"
    assert [e["game"] for e in summary.errors] == [SOON]
    assert {s.game_id for s in db.scalars(select(PropSnapshot))} == {DAY, FAR}


def test_run_stops_when_the_budget_runs_out(db: Session) -> None:
    odds = FakeOdds(standard_responses(), budget=2 * len(MAIN_MARKETS))
    summary = make_capture(db, odds).run()

    assert summary.status == "partial"
    assert summary.games_polled == 2
    assert summary.deferred == [FAR]
    assert odds.polled == ["ev-soon", "ev-day"]
    run = db.scalars(select(IngestionRun)).one()
    assert run.details["deferred"] == [FAR]


def test_a_spent_budget_skips_the_run_without_recording_one(db: Session) -> None:
    odds = FakeOdds(standard_responses(), budget=15, used_today=10)  # a poll costs 9
    summary = make_capture(db, odds).run()

    assert summary.status == "budget_exhausted"
    assert summary.deferred == [SOON, DAY, FAR]
    assert odds.polled == []
    assert db.scalar(select(func.count()).select_from(IngestionRun)) == 0


def test_a_bad_tier_is_rejected_before_any_call(db: Session) -> None:
    odds = FakeOdds(standard_responses())
    with pytest.raises(ValueError, match="no polling tier"):
        make_capture(db, odds).run("7m")
    assert odds.polled == []


# Resolving names and shaping rows --------------------------------------------------------------


def test_unmatched_names_are_recorded_and_not_stored(db: Session) -> None:
    responses = standard_responses()
    responses["ev-soon"] = [
        *soon_props(),
        quote("ev-soon", "player_rush_yds", "Nobody Special", "over", 30.5, -110),
    ]
    summary = make_capture(db, FakeOdds(responses)).run("15m")

    assert summary.unmatched_names == 1
    assert summary.new_snapshots == 2
    unmatched = db.scalars(select(UnmatchedName)).one()
    assert (unmatched.raw_name, unmatched.game_id, unmatched.position_hint) == (
        "Nobody Special",
        SOON,
        "QB,RB,WR",
    )
    run = db.scalars(select(IngestionRun)).one()
    assert run.details["games"][SOON]["unmatched_names"] == ["Nobody Special"]


def test_resolved_names_are_saved_as_aliases(db: Session) -> None:
    make_capture(db, FakeOdds(standard_responses())).run("15m")
    aliases = {a.raw_name: a.player_id for a in db.scalars(select(PlayerAlias))}
    assert aliases == {"Isiah Pacheco": 1, "Travis Kelce": 2}


def test_a_lone_under_price_is_skipped(db: Session) -> None:
    responses = standard_responses()
    responses["ev-soon"] = [
        quote("ev-soon", "player_rush_yds", "Isiah Pacheco", "under", 64.5, -105)
    ]
    summary = make_capture(db, FakeOdds(responses)).run("15m")

    assert summary.new_snapshots == 0
    assert db.scalars(select(IngestionRun)).one().details["one_sided_quotes"] == 1


def test_alternate_thresholds_are_separate_rows(db: Session) -> None:
    responses = standard_responses()
    responses["ev-soon"] = [
        quote("ev-soon", "player_rush_yds_alternate", "Isiah Pacheco", "over", 49.5, -250),
        quote("ev-soon", "player_rush_yds_alternate", "Isiah Pacheco", "over", 74.5, 150),
        quote("ev-soon", "player_rush_yds_alternate", "Isiah Pacheco", "over", 99.5, 450),
    ]
    summary = make_capture(db, FakeOdds(responses)).run("15m")

    assert summary.new_snapshots == 3
    lines = sorted(s.line for s in db.scalars(select(PropSnapshot)) if s.line is not None)
    assert lines == [49.5, 74.5, 99.5]
    assert all(s.under_price is None for s in db.scalars(select(PropSnapshot)))


def test_an_unseeded_book_is_registered(db: Session) -> None:
    responses = standard_responses()
    responses["ev-soon"] = [
        quote("ev-soon", "player_rush_yds", "Isiah Pacheco", "over", 64.5, -115, book="newbook"),
        quote("ev-soon", "player_rush_yds", "Isiah Pacheco", "under", 64.5, -105, book="newbook"),
    ]
    summary = make_capture(db, FakeOdds(responses)).run("15m")

    assert summary.new_snapshots == 1
    book = db.get(Book, "newbook")
    assert book is not None
    assert (book.display_name, book.sharpness_weight, book.active) == ("newbook", 1.0, True)
    assert db.scalars(select(IngestionRun)).one().details["new_books"] == ["newbook"]


def test_an_unknown_market_is_skipped(db: Session) -> None:
    responses = standard_responses()
    responses["ev-soon"] = [quote("ev-soon", "player_sacks", "Isiah Pacheco", "over", 0.5, -110)]
    summary = make_capture(db, FakeOdds(responses)).run("15m")

    assert summary.new_snapshots == 0
    assert db.scalars(select(IngestionRun)).one().details["unknown_market_quotes"] == 1


# Linking, the hook, and budget bookkeeping -----------------------------------------------------


def test_runs_link_new_games_to_provider_events_first(db: Session) -> None:
    game = db.get(Game, "2026_08_DAL_PHI")
    assert game is not None and game.odds_event_id is None
    event = OddsEvent("ev-new", "Philadelphia Eagles", "Dallas Cowboys", game.kickoff_utc)
    responses = {
        **standard_responses(),
        "ev-new": [quote("ev-new", "player_pass_yds", "Jordan Love", "over", 1.5, -110)],
    }
    odds = FakeOdds(responses, events=[event])

    summary = make_capture(db, odds).run()
    db.expire_all()

    assert db.get(Game, "2026_08_DAL_PHI").odds_event_id == "ev-new"  # type: ignore[union-attr]
    assert "ev-new" in odds.polled
    assert summary.unlinked_games == 0


def test_the_recompute_hook_hears_about_new_snapshots_only(db: Session) -> None:
    heard: list[RecomputeRequest] = []
    capture = make_capture(db, FakeOdds(standard_responses()), hook=heard.append)
    capture.run("15m")
    # The game, the players with new rows, and the moment the provider says it was taken.
    assert heard == [RecomputeRequest(SOON, (1, 2), NOW)]
    capture.run("15m")  # nothing changed, so nothing to recompute
    assert len(heard) == 1


def test_a_failing_hook_does_not_fail_the_run(db: Session) -> None:
    def boom(request: RecomputeRequest) -> None:
        raise RuntimeError("queue is down")

    summary = make_capture(db, FakeOdds(standard_responses()), hook=boom).run()
    assert summary.status == "ok"
    assert snapshot_count(db) == 4


def test_credits_used_today_sums_only_todays_live_runs(db: Session) -> None:
    midnight = NOW.replace(hour=0, minute=0)
    db.add_all(
        [
            IngestionRun(
                kind="odds_live", started_at=midnight + timedelta(hours=1), credits_used=9
            ),
            IngestionRun(
                kind="odds_live", started_at=midnight + timedelta(hours=2), credits_used=6
            ),
            IngestionRun(
                kind="odds_live", started_at=midnight - timedelta(minutes=1), credits_used=99
            ),
            IngestionRun(
                kind="nflverse", started_at=midnight + timedelta(hours=3), credits_used=50
            ),
        ]
    )
    db.commit()
    assert credits_used_today(db, NOW) == 15
    assert has_previous_runs(db)


def test_has_previous_runs_is_false_on_a_fresh_database(db: Session) -> None:
    assert not has_previous_runs(db)


# End to end with the real adapter --------------------------------------------------------------


def test_end_to_end_with_the_odds_api_adapter_and_recorded_responses(db: Session) -> None:
    """The adapter, the resolver, dedupe and the run record working together."""
    db.add(
        Game(
            id="2026_08_KC_LV_e2e",
            season=2026,
            week=8,
            season_type="REG",
            kickoff_utc=datetime(2026, 10, 25, 20, 25, tzinfo=UTC),
            home_abbr="LV",
            away_abbr="KC",
        )
    )
    db.commit()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=json.loads((FIXTURES / "events.json").read_text()))
        body = json.loads((FIXTURES / "event_odds.json").read_text())
        return httpx.Response(
            200,
            json=body,
            headers={"x-requests-last": "3", "x-requests-remaining": "497", "x-requests-used": "3"},
        )

    provider = TheOddsApiProvider(
        api_key="test-key",
        daily_credit_budget=15,
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    markets = ("player_rush_yds", "player_anytime_td", "player_rush_yds_alternate")
    capture = make_capture(db, provider, markets=markets)
    summary = capture.run("3h")

    assert summary.status == "ok"
    assert (summary.games_polled, summary.credits_used, summary.credits_remaining) == (1, 3, 497)
    # DraftKings: Pacheco and White rush, Pacheco and Kelce TD. FanDuel: Pacheco rush, 3 alternates.
    assert summary.new_snapshots == 8
    assert summary.unmatched_names == 0
    polled = [r for r in requests if "/odds" in r.url.path]
    assert len(polled) == 1
    assert polled[0].url.params["markets"] == ",".join(markets)

    game = db.scalars(select(Game).where(Game.id == "2026_08_KC_LV_e2e")).one()
    assert game.odds_event_id == "e912304de2b2ce35b473ce2ecd3d1502"
    assert make_capture(db, provider, markets=markets).run("3h").new_snapshots == 0
