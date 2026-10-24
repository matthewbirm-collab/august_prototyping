from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Game, Player, PropSnapshot
from db.seed import seed
from db.snapshots import SnapshotQuote, insert_prop_snapshots

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(session: Session) -> Iterator[Session]:
    seed(session)
    session.add(
        Game(
            id="2026_08_KC_LV",
            season=2026,
            week=8,
            season_type="REG",
            kickoff_utc=T0 + timedelta(days=1),
            home_abbr="LV",
            away_abbr="KC",
        )
    )
    session.add(Player(id=1, full_name="Test Runner", position="RB", team_abbr="KC"))
    session.commit()
    yield session


def _quote(minutes: int = 0, **changes: Any) -> SnapshotQuote:
    base = SnapshotQuote(
        captured_at=T0 + timedelta(minutes=minutes),
        game_id="2026_08_KC_LV",
        book_key="draftkings",
        market_key="player_rush_yds",
        player_id=1,
        line=64.5,
        over_price=-115,
        under_price=-105,
        source="live",
    )
    return replace(base, **changes)


def _stored(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(PropSnapshot)) or 0


def test_first_quote_is_stored(db: Session) -> None:
    assert insert_prop_snapshots(db, [_quote()]) == 1
    db.commit()
    assert _stored(db) == 1


def test_unchanged_quote_is_skipped(db: Session) -> None:
    insert_prop_snapshots(db, [_quote()])
    db.commit()
    assert insert_prop_snapshots(db, [_quote(minutes=15)]) == 0
    assert _stored(db) == 1


def test_price_change_is_stored(db: Session) -> None:
    insert_prop_snapshots(db, [_quote()])
    assert insert_prop_snapshots(db, [_quote(minutes=15, over_price=-125, under_price=105)]) == 1
    assert _stored(db) == 2


def test_main_line_move_and_move_back_are_both_stored(db: Session) -> None:
    inserted = insert_prop_snapshots(
        db, [_quote(), _quote(minutes=15, line=67.5), _quote(minutes=30)]
    )
    assert inserted == 3


def test_duplicates_within_one_batch_are_skipped(db: Session) -> None:
    assert insert_prop_snapshots(db, [_quote(), _quote(minutes=5), _quote(minutes=10)]) == 1


def test_each_alternate_threshold_is_its_own_series(db: Session) -> None:
    alt: dict[str, Any] = {"market_key": "player_rush_yds_alternate", "under_price": None}
    first = [_quote(line=50, over_price=-200, **alt), _quote(line=75, over_price=150, **alt)]
    assert insert_prop_snapshots(db, first) == 2

    later = [
        _quote(minutes=15, line=50, over_price=-200, **alt),
        _quote(minutes=15, line=75, over_price=170, **alt),
    ]
    assert insert_prop_snapshots(db, later) == 1


def test_one_sided_anytime_td_quote(db: Session) -> None:
    td: dict[str, Any] = {"market_key": "player_anytime_td", "line": None, "under_price": None}
    assert insert_prop_snapshots(db, [_quote(over_price=140, **td)]) == 1
    assert insert_prop_snapshots(db, [_quote(minutes=15, over_price=140, **td)]) == 0


def test_books_are_separate_series(db: Session) -> None:
    assert insert_prop_snapshots(db, [_quote(), _quote(book_key="fanduel")]) == 2


def test_empty_batch() -> None:
    assert insert_prop_snapshots(None, []) == 0  # type: ignore[arg-type]


def test_an_earlier_snapshot_stored_after_a_later_one_is_not_dropped(db: Session) -> None:
    """A backfill can add history out of order. Each quote is compared with the row before it in
    time, so "the latest quote at or before T" stays right."""
    insert_prop_snapshots(db, [_quote(minutes=60, over_price=-125, under_price=105)])
    stored = insert_prop_snapshots(
        db,
        [
            _quote(minutes=0),  # before the move: differs from the row that follows
            _quote(minutes=30, over_price=-125, under_price=105),  # equals the later row
        ],
    )
    # Both are stored: nothing precedes the first, and the second follows a different price.
    assert stored == 2

    again = insert_prop_snapshots(db, [_quote(minutes=45, over_price=-125, under_price=105)])
    assert again == 0  # unchanged since the 30-minute row
