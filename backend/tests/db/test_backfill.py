from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import (
    BackfillProgress,
    Game,
    IngestionRun,
    PropSnapshot,
    UnmatchedName,
)
from ingest.backfill import Backfill, StopRunError, format_report
from tests.db.backfill_support import (
    EVENT_A,
    EVENT_B,
    GAME_A,
    GAME_B,
    KICKOFF_A,
    PER_CALL,
    FakeHistory,
    build_world,
    events_for_games,
    make_config,
)

# Props go up five days out, on a scan point (nine days out plus 16 six-hour steps).
POSTED_A = KICKOFF_A - timedelta(hours=120)


@pytest.fixture
def db(session: Session) -> Iterator[Session]:
    yield build_world(session)


def backfill(db: Session, provider: FakeHistory, **config: object) -> Backfill:
    return Backfill(db, provider, make_config(**config), sleep=lambda _: None)


def progress(db: Session, game_id: str = GAME_A) -> dict[str, BackfillProgress]:
    rows = db.scalars(select(BackfillProgress).where(BackfillProgress.game_id == game_id))
    return {row.label: row for row in rows}


def only_a(posted: datetime | None = POSTED_A, **fake: object) -> FakeHistory:
    return FakeHistory(posted={EVENT_A: posted, EVENT_B: None}, **fake)  # type: ignore[arg-type]


# A normal game ---------------------------------------------------------------------------------


def test_a_game_gets_six_snapshots_and_pays_only_for_those_with_props(db: Session) -> None:
    provider = only_a()
    summary = backfill(db, provider).run([2025], weeks=[8])

    assert summary.status == "ok"
    rows = progress(db)
    assert {label: row.status for label, row in rows.items()} == dict.fromkeys(
        ("open", "t72h", "t24h", "t6h", "t1h", "close"), "done"
    )

    free = [c for c in provider.calls if c.event_id == EVENT_A and not c.charged]
    paid = [c for c in provider.calls if c.event_id == EVENT_A and c.charged]
    assert len(paid) == 6
    assert len(free) == 16  # the open search, nine days out to just before props went up
    assert all(c.at < POSTED_A for c in free)
    assert sum(c.charged for c in paid) == 6 * PER_CALL


def test_open_is_the_first_snapshot_with_props_and_the_rest_are_at_their_offsets(
    db: Session,
) -> None:
    backfill(db, only_a()).run([2025])
    rows = progress(db)

    assert rows["open"].snapshot_at == POSTED_A
    for label, hours in {"t72h": 72, "t24h": 24, "t6h": 6, "t1h": 1}.items():
        assert rows[label].target_at == KICKOFF_A - timedelta(hours=hours)
        assert rows[label].snapshot_at == rows[label].target_at
    assert rows["close"].target_at == KICKOFF_A - timedelta(minutes=1)
    assert rows["close"].snapshot_at == KICKOFF_A - timedelta(minutes=5)


def test_quotes_are_stored_as_historical_at_the_snapshot_time(db: Session) -> None:
    summary = backfill(db, only_a()).run([2025])
    rows = list(db.scalars(select(PropSnapshot).order_by(PropSnapshot.captured_at)))

    assert {r.source for r in rows} == {"historical"}
    assert {r.game_id for r in rows} == {GAME_A}
    assert rows[0].captured_at == POSTED_A
    # Unchanged quotes are stored once: one rush row and one anytime-TD row.
    assert len(rows) == 2
    assert summary.quotes == 6 * 4  # six snapshots of four quotes
    assert summary.new_snapshots == 2


def test_a_moved_line_is_stored_from_the_snapshot_where_it_moved(db: Session) -> None:
    moved_at = KICKOFF_A - timedelta(hours=30)
    provider = only_a(price=lambda event, snapshot: -110 if snapshot < moved_at else -125)
    backfill(db, provider).run([2025])

    rush = list(
        db.scalars(
            select(PropSnapshot)
            .where(PropSnapshot.market_key == "player_rush_yds")
            .order_by(PropSnapshot.captured_at)
        )
    )
    assert [(r.captured_at, r.over_price) for r in rush] == [
        (POSTED_A, -110),
        (KICKOFF_A - timedelta(hours=24), -125),
    ]


def test_the_run_is_recorded(db: Session) -> None:
    provider = FakeHistory(posted={EVENT_A: POSTED_A, EVENT_B: POSTED_A})
    summary = backfill(db, provider).run([2025])

    run = db.scalars(select(IngestionRun)).one()
    assert (run.kind, run.status, run.events) == ("odds_historical", "ok", 2)
    assert run.credits_used == provider.used == 12 * PER_CALL
    assert run.details["seasons"] == [2025]
    assert run.details["snapshots"] == {"done": 12}
    assert run.details["stopped"] is None
    assert summary.run_id == run.id
    assert all(r.ingestion_run_id == run.id for r in progress(db).values())


# Props that go up late, or never ---------------------------------------------------------------


def test_props_posted_late_leave_the_early_snapshots_empty(db: Session) -> None:
    provider = only_a(posted=KICKOFF_A - timedelta(hours=30))
    backfill(db, provider).run([2025])
    rows = progress(db)

    assert rows["t72h"].status == "empty"
    assert rows["t72h"].snapshot_at is None
    assert rows["open"].snapshot_at == KICKOFF_A - timedelta(hours=30)
    assert [rows[k].status for k in ("t24h", "t6h", "t1h", "close")] == ["done"] * 4
    assert provider.used == 5 * PER_CALL  # the empty t72h probe cost nothing


def test_a_game_with_no_props_costs_nothing_and_is_not_searched_again(db: Session) -> None:
    provider = FakeHistory(posted={EVENT_A: None, EVENT_B: None})
    first = backfill(db, provider).run([2025])

    assert first.status == "ok"
    assert provider.used == 0
    assert {r.status for r in progress(db).values()} == {"empty"}
    assert set(progress(db)) == {"open", "t72h", "t24h", "t6h", "t1h", "close"}

    calls = len(provider.calls)
    again = backfill(db, provider).run([2025])
    assert again.status == "idle"
    assert len(provider.calls) == calls


def test_close_falls_back_to_earlier_times_when_props_are_pulled_before_kickoff(
    db: Session,
) -> None:
    provider = only_a(pulled={EVENT_A: KICKOFF_A - timedelta(minutes=15)})
    backfill(db, provider).run([2025])
    close = progress(db)["close"]

    assert close.status == "done"
    assert close.target_at == KICKOFF_A - timedelta(minutes=30)
    assert close.snapshot_at == KICKOFF_A - timedelta(minutes=30)


def test_a_later_target_on_the_same_snapshot_is_not_bought_twice_and_old_data_is_flagged(
    db: Session,
) -> None:
    kickoff_b = datetime(2025, 10, 19, 20, 0, tzinfo=UTC)
    provider = FakeHistory(
        posted={EVENT_A: None, EVENT_B: datetime(2025, 10, 14, 0, 0, tzinfo=UTC)},
        interval=timedelta(hours=12),
    )
    backfill(db, provider).run([2025])
    rows = progress(db, GAME_B)

    noon = datetime(2025, 10, 19, 12, 0, tzinfo=UTC)
    assert rows["open"].status == "done"
    assert rows["t6h"].snapshot_at == noon
    assert rows["t6h"].status == "stale"  # 14:00 target, noon snapshot: older than 90 minutes
    # One hour out and the close land on the same noon snapshot, so they cost nothing.
    assert (rows["t1h"].snapshot_at, rows["t1h"].credits) == (noon, 0)
    assert (rows["close"].snapshot_at, rows["close"].credits) == (noon, 0)
    assert rows["close"].target_at == kickoff_b - timedelta(minutes=1)
    b_paid = [c for c in provider.calls if c.event_id == EVENT_B and c.charged]
    assert len(b_paid) == 4  # open, t72h, t24h, t6h
    assert not any(c.at == kickoff_b - timedelta(hours=1) for c in provider.calls)


# Resuming, stopping and limits -----------------------------------------------------------------


def test_a_credit_cap_stops_the_run_and_a_rerun_buys_only_what_is_missing(db: Session) -> None:
    provider = only_a(budget=3 * PER_CALL)
    first = backfill(db, provider).run([2025])

    assert first.status == "partial"
    assert first.stopped == "credit cap"
    assert set(progress(db)) == {"open", "t72h", "t24h"}
    assert db.scalars(select(IngestionRun)).one().details["stopped"] == "credit cap"

    provider.budget = 10**9
    second = backfill(db, provider).run([2025])

    assert second.status == "ok"
    assert set(progress(db)) == {"open", "t72h", "t24h", "t6h", "t1h", "close"}
    assert provider.paid_calls == 6  # nothing bought twice
    assert provider.used == 6 * PER_CALL
    assert [c.at for c in provider.calls if c.charged].count(KICKOFF_A - timedelta(hours=72)) == 1


def test_the_plan_reserve_is_left_untouched(db: Session) -> None:
    provider = only_a(plan_remaining=100)
    summary = backfill(db, provider, reserve=50).run([2025])

    assert summary.stopped == "reserve"
    assert summary.status == "partial"
    assert set(progress(db)) == {"open", "t72h"}
    assert provider.plan_remaining - provider.used >= 50  # type: ignore[operator]


def test_one_game_failing_does_not_stop_the_others_and_is_retried_next_time(db: Session) -> None:
    provider = FakeHistory(
        posted={EVENT_A: POSTED_A, EVENT_B: POSTED_A}, errors={EVENT_A: "HTTP 503: upstream down"}
    )
    summary = backfill(db, provider).run([2025])

    assert summary.status == "partial"
    assert [e["game"] for e in summary.errors] == [GAME_A]
    assert progress(db) == {}
    assert len(progress(db, GAME_B)) == 6

    del provider.errors[EVENT_A]
    again = backfill(db, provider).run([2025])
    assert again.status == "ok"
    assert len(progress(db)) == 6
    assert again.games == 1  # only the game that was missing


def test_a_subset_of_snapshots_skips_the_open_search(db: Session) -> None:
    provider = only_a()
    backfill(db, provider, labels=("t24h", "t1h", "close")).run([2025])

    a_calls = [c for c in provider.calls if c.event_id == EVENT_A]
    assert len(a_calls) == 3
    assert min(c.at for c in a_calls) == KICKOFF_A - timedelta(hours=24)
    assert set(progress(db)) == {"t24h", "t1h", "close"}


def test_snapshots_added_later_are_stored_in_time_order_correctly(db: Session) -> None:
    moved_at = KICKOFF_A - timedelta(hours=30)
    provider = only_a(price=lambda event, snapshot: -110 if snapshot < moved_at else -125)
    backfill(db, provider, labels=("t1h", "close")).run([2025])  # the late snapshots first
    backfill(db, provider, labels=("t72h", "t24h")).run([2025])  # then the earlier ones

    rush = list(
        db.scalars(
            select(PropSnapshot)
            .where(PropSnapshot.market_key == "player_rush_yds")
            .order_by(PropSnapshot.captured_at)
        )
    )

    # The earlier snapshots must not be dropped for matching the later rows already stored. The
    # t1h row stored first stays (it is now redundant but harmless).
    def price_at(moment: datetime) -> int:
        return [r.over_price for r in rush if r.captured_at <= moment][-1]

    assert [r.captured_at for r in rush][:2] == [
        KICKOFF_A - timedelta(hours=72),
        KICKOFF_A - timedelta(hours=24),
    ]
    assert price_at(KICKOFF_A - timedelta(hours=48)) == -110
    assert price_at(KICKOFF_A - timedelta(hours=12)) == -125
    assert price_at(KICKOFF_A - timedelta(minutes=1)) == -125


def test_requests_are_spaced_by_the_rate_limit(db: Session) -> None:
    sleeps: list[float] = []
    provider = only_a()
    Backfill(db, provider, make_config(max_rps=2), sleep=sleeps.append, monotonic=lambda: 0.0).run(
        [2025]
    )
    assert len(sleeps) == len(provider.calls) - 1
    assert set(sleeps) == {0.5}


def test_unlinked_games_are_left_alone(session: Session) -> None:
    db = build_world(session, linked=False)
    provider = only_a()
    summary = backfill(db, provider).run([2025])

    assert summary.status == "idle"
    assert provider.calls == []
    assert backfill(db, provider).estimate([2025]).unlinked_games == 2


# Names and reporting ---------------------------------------------------------------------------


def test_unmatched_names_are_counted_and_reported_against_the_gate(db: Session) -> None:
    provider = FakeHistory(
        posted={EVENT_A: POSTED_A, EVENT_B: POSTED_A},
        quotes_for={EVENT_A: ["Nobody Special", "Travis Kelce"]},
    )
    runner = backfill(db, provider)
    summary = runner.run([2025])

    assert summary.unmatched_names == 1
    unmatched = db.scalars(select(UnmatchedName)).one()
    assert (unmatched.raw_name, unmatched.game_id) == ("Nobody Special", GAME_A)

    [report] = runner.report([2025])
    assert (report.games, report.linked) == (2, 2)
    assert report.statuses == {"done": 12}
    # Kelce, Love and Swift matched; one name did not.
    assert (report.unmatched.unmatched, report.unmatched.matched_players) == (1, 3)
    text = format_report([report])
    assert "2025: 2 games (2 linked to events). Snapshots: 12 done, 0 empty, 0 stale." in text
    assert "1 of 4 (25.0%), OVER the 2% gate" in text


def test_the_estimate_shrinks_as_snapshots_are_finished(db: Session) -> None:
    provider = only_a()
    runner = backfill(db, provider)
    before = runner.estimate([2025])
    assert (before.games, before.calls, before.snapshot_credits) == (2, 12, 12 * PER_CALL)

    runner.run([2025])
    after = runner.estimate([2025])
    assert after.done == 12
    assert (after.calls, after.snapshot_credits) == (0, 0)


# Linking games to historical events ------------------------------------------------------------


def test_games_are_linked_with_one_call_per_week_and_one_more_for_a_missed_game(
    session: Session,
) -> None:
    db = build_world(session, linked=False)
    provider = FakeHistory(posted={}, events=events_for_games(), narrow_only={EVENT_B})
    report = backfill(db, provider).link_events([2025])

    assert (report.weeks, report.linked, report.unlinked) == (1, 2, [])
    assert report.credits == 2
    assert db.get(Game, GAME_A).odds_event_id == EVENT_A  # type: ignore[union-attr]
    assert db.get(Game, GAME_B).odds_event_id == EVENT_B  # type: ignore[union-attr]

    wide, narrow = provider.event_calls
    assert wide[2] - wide[1] > timedelta(hours=6)
    assert narrow[2] - narrow[1] < timedelta(hours=6)
    assert wide[0] == KICKOFF_A - timedelta(days=1)

    assert backfill(db, provider).link_events([2025]).credits == 0  # nothing left to link


def test_games_the_provider_never_listed_are_reported(session: Session) -> None:
    db = build_world(session, linked=False)
    provider = FakeHistory(
        posted={}, events=[e for e in events_for_games() if e.event_id == EVENT_A]
    )
    report = backfill(db, provider).link_events([2025])

    assert (report.linked, report.unlinked) == (1, [GAME_B])
    assert report.credits == 1  # a call that finds nothing is free


def test_linking_stops_at_the_credit_cap(session: Session) -> None:
    db = build_world(session, linked=False)
    provider = FakeHistory(posted={}, events=events_for_games(), budget=0)
    with pytest.raises(StopRunError, match="credit cap"):
        backfill(db, provider).link_events([2025])
