"""Turn a provider's quotes for one game into stored `prop_snapshots` rows.

Shared by the live capture job and the historical backfill: resolve player names, join each
over/under (or yes/no) pair into one row, register sportsbooks we haven't seen, and store only
the rows that changed since the latest one in their series. The caller commits.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from db.models import Book, Game, Market
from db.snapshots import SnapshotQuote, store_prop_snapshots
from ingest.odds.provider import PropQuote
from ingest.resolver import GameContext, PlayerResolver, position_hint_for_market

logger = logging.getLogger(__name__)

Source = Literal["live", "historical"]
SeriesKey = tuple[str, str, int, float | None]


@dataclass
class GameResult:
    quotes: int = 0
    new_snapshots: int = 0
    unmatched: set[str] = field(default_factory=set)
    one_sided: int = 0
    unknown_market: int = 0
    new_books: list[str] = field(default_factory=list)
    player_ids: set[int] = field(default_factory=set)


class QuoteStore:
    def __init__(self, session: Session, resolver: PlayerResolver, source: Source) -> None:
        self._session = session
        self._resolver = resolver
        self._source = source

    def store(self, game: Game, quotes: Sequence[PropQuote], run_id: int | None) -> GameResult:
        result = GameResult(quotes=len(quotes))
        known_markets = set(self._session.scalars(select(Market.key)))
        result.new_books = self._ensure_books({q.book for q in quotes})
        resolver = self._resolver.for_game(GameContext.from_game(game))

        prices: dict[SeriesKey, dict[str, int]] = {}
        captured: dict[SeriesKey, datetime] = {}
        for quote in quotes:
            if quote.market not in known_markets:
                result.unknown_market += 1
                continue
            resolution = resolver.resolve(
                quote.player_name_raw, position_hint=position_hint_for_market(quote.market)
            )
            if resolution.player_id is None:
                result.unmatched.add(quote.player_name_raw)
                continue
            key = (quote.book, quote.market, resolution.player_id, quote.line)
            prices.setdefault(key, {})[quote.side] = quote.price
            captured[key] = quote.captured_at

        snapshots: list[SnapshotQuote] = []
        for key, sides in prices.items():
            book, market, player_id, line = key
            over = sides.get("over", sides.get("yes"))
            if over is None:
                result.one_sided += 1  # only an under/no price: nothing to anchor the row on
                continue
            snapshots.append(
                SnapshotQuote(
                    captured_at=captured[key],
                    game_id=game.id,
                    book_key=book,
                    market_key=market,
                    player_id=player_id,
                    line=line,
                    over_price=over,
                    under_price=sides.get("under", sides.get("no")),
                    source=self._source,
                    ingestion_run_id=run_id,
                )
            )
        stored = store_prop_snapshots(self._session, snapshots)
        result.new_snapshots = len(stored)
        result.player_ids = {q.player_id for q in stored}
        return result

    def _ensure_books(self, keys: set[str]) -> list[str]:
        """Register a book the provider returns that we haven't seeded (default weight)."""
        known = set(self._session.scalars(select(Book.key).where(Book.key.in_(keys))))
        new = sorted(keys - known)
        if new:
            logger.warning("new sportsbook from provider", extra={"books": new})
            self._session.execute(
                insert(Book)
                .values([{"key": key, "display_name": key} for key in new])
                .on_conflict_do_nothing()
            )
        return new
