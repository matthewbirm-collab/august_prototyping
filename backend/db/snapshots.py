"""Write prop snapshots, skipping quotes that haven't changed since the last one in their series.

A series is (game, book, market, player) for main markets. Alternate markets carry many
thresholds per player, so each threshold is its own series: the line joins the key.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import Row, case, insert, select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from db.models import Market, PropSnapshot

SeriesKey = tuple[str, str, str, int, float | None]


@dataclass(frozen=True)
class SnapshotQuote:
    captured_at: datetime
    game_id: str
    book_key: str
    market_key: str
    player_id: int
    line: float | None
    over_price: int
    under_price: int | None
    source: Literal["live", "historical"]
    ingestion_run_id: int | None = None

    def payload_hash(self) -> bytes:
        """SHA-256 over everything that identifies the quote, excluding when it was captured."""
        payload = [
            self.game_id,
            self.book_key,
            self.market_key,
            self.player_id,
            self.line,
            self.over_price,
            self.under_price,
        ]
        return hashlib.sha256(json.dumps(payload).encode()).digest()

    def series_key(self, is_alternate: bool) -> SeriesKey:
        line = self.line if is_alternate else None
        return (self.game_id, self.book_key, self.market_key, self.player_id, line)


def insert_prop_snapshots(session: Session, quotes: Sequence[SnapshotQuote]) -> int:
    """Insert quotes whose payload differs from the latest stored row in their series.

    Quotes are processed in `captured_at` order, so a batch holding several captures of one
    series is deduplicated against itself too. Each quote is compared with the latest row at or
    before the batch's newest `captured_at`, so a historical snapshot stored after later ones
    (an out-of-order backfill) isn't dropped for matching a row that comes after it. Returns the
    number of rows inserted; the caller commits.
    """
    return len(store_prop_snapshots(session, quotes))


def store_prop_snapshots(session: Session, quotes: Sequence[SnapshotQuote]) -> list[SnapshotQuote]:
    """Like `insert_prop_snapshots`, but returns the quotes that were stored."""
    if not quotes:
        return []

    alternates = _alternate_markets(session, {q.market_key for q in quotes})
    latest = _latest_hashes(session, quotes, alternates)

    stored: list[SnapshotQuote] = []
    rows = []
    for quote in sorted(quotes, key=lambda q: q.captured_at):
        key = quote.series_key(quote.market_key in alternates)
        digest = quote.payload_hash()
        if latest.get(key) == digest:
            continue
        latest[key] = digest
        stored.append(quote)
        rows.append(
            {
                "captured_at": quote.captured_at,
                "game_id": quote.game_id,
                "book_key": quote.book_key,
                "market_key": quote.market_key,
                "player_id": quote.player_id,
                "line": quote.line,
                "over_price": quote.over_price,
                "under_price": quote.under_price,
                "source": quote.source,
                "payload_hash": digest,
                "ingestion_run_id": quote.ingestion_run_id,
            }
        )

    if rows:
        session.execute(insert(PropSnapshot), rows)
    return stored


def _alternate_markets(session: Session, market_keys: set[str]) -> set[str]:
    stmt = select(Market.key).where(Market.key.in_(market_keys), Market.is_alternate)
    return set(session.scalars(stmt))


def _latest_hashes(
    session: Session, quotes: Sequence[SnapshotQuote], alternates: set[str]
) -> dict[SeriesKey, bytes]:
    """Latest stored payload hash for every series the batch touches."""
    series_line = case((PropSnapshot.market_key.in_(alternates), PropSnapshot.line), else_=None)
    group_columns = (
        PropSnapshot.game_id,
        PropSnapshot.book_key,
        PropSnapshot.market_key,
        PropSnapshot.player_id,
    )
    groups = [(q.game_id, q.book_key, q.market_key, q.player_id) for q in quotes]
    stmt = (
        select(*group_columns, series_line.label("series_line"), PropSnapshot.payload_hash)
        .where(
            tuple_(*group_columns).in_(set(groups)),
            PropSnapshot.captured_at <= max(q.captured_at for q in quotes),
        )
        .ext(distinct_on(*group_columns, series_line))
        .order_by(*group_columns, series_line, PropSnapshot.captured_at.desc())
    )
    return {_key(row): row.payload_hash for row in session.execute(stmt)}


def _key(row: Row[tuple[str, str, str, int, float | None, bytes]]) -> SeriesKey:
    return (row.game_id, row.book_key, row.market_key, row.player_id, row.series_line)
