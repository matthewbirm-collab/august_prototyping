"""Compute consensus lines from stored quotes and write them to `consensus_snapshots`.

Runs after each game's live capture (it is the recompute hook), and can be called for any moment
in the past, since it only reads quotes taken no later than `as_of`. Main markets only: alternate
lines are mostly one-sided thresholds that `/p3-distributions` uses for the tails.
"""

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.distribution_params import (
    DEFAULT_PATH,
    DistributionParams,
    load_distribution_params,
)
from app.settings import Settings
from db.models import Book, ConsensusSnapshot, Market, Player, PropSnapshot
from engine.consensus import (
    DEFAULT_LINE_CV,
    STUB_LINE_CV,
    BookQuote,
    Consensus,
    ConsensusConfig,
    consensus,
)
from engine.distributions import FittedLineModel
from ingest.recompute import RecomputeRequest

logger = logging.getLogger(__name__)

TOLERANCE = 1e-9


@dataclass(frozen=True)
class JobConfig:
    consensus: ConsensusConfig = field(default_factory=ConsensusConfig)
    line_cv: Mapping[str, float] = field(default_factory=lambda: dict(STUB_LINE_CV))
    """Spread of each stat as a fraction of its line, by `markets.stat`: the fallback curve for a
    stat or position with no fitted shape."""
    params: DistributionParams | None = None
    """Fitted shapes by stat and position. None means every stat uses the fallback curve."""

    @classmethod
    def from_settings(cls, settings: Settings) -> "JobConfig":
        return cls(
            ConsensusConfig(
                method=settings.devig_method,
                stale_after=timedelta(minutes=settings.stale_quote_minutes),
                half_life=timedelta(minutes=settings.consensus_half_life_minutes),
                one_sided_overround=settings.one_sided_overround,
            ),
            params=load_distribution_params(settings.distribution_params_path or DEFAULT_PATH),
        )


@dataclass
class JobResult:
    written: int = 0
    unchanged: int = 0
    no_consensus: int = 0


class ConsensusJob:
    """Callable as the capture's recompute hook."""

    def __init__(self, session: Session, config: JobConfig | None = None) -> None:
        self._session = session
        self._cfg = config or JobConfig()

    def __call__(self, request: RecomputeRequest) -> None:
        result = self.recompute(request.game_id, request.player_ids, request.as_of)
        logger.info(
            "consensus recomputed",
            extra={
                "game": request.game_id,
                "written": result.written,
                "unchanged": result.unchanged,
                "no_consensus": result.no_consensus,
            },
        )

    def recompute(
        self, game_id: str, player_ids: Iterable[int] | None, as_of: datetime
    ) -> JobResult:
        """Write a consensus row for each (player, main market) of the game whose consensus as
        of `as_of` differs from the latest one stored. `player_ids` None means every player with
        a recent quote. Commits."""
        result = JobResult()
        weights = dict(self._session.execute(select(Book.key, Book.sharpness_weight)).all())
        stats = dict(self._session.execute(select(Market.key, Market.stat)).all())
        groups = self._quotes(game_id, player_ids, as_of)
        previous = self._latest_consensus(game_id, as_of)
        positions = dict(
            self._session.execute(
                select(Player.id, Player.position).where(Player.id.in_({p for _, p in groups}))
            ).all()
        )

        for (market_key, player_id), quotes in sorted(groups.items()):
            config = self._consensus_config(stats.get(market_key, ""), positions.get(player_id))
            answer = consensus(quotes, weights, config, as_of)
            if answer is None:
                result.no_consensus += 1
                continue
            if _same(previous.get((market_key, player_id)), answer):
                result.unchanged += 1
                continue
            self._session.add(
                ConsensusSnapshot(
                    captured_at=as_of,
                    game_id=game_id,
                    player_id=player_id,
                    market_key=market_key,
                    fair_line=answer.fair_line,
                    fair_p_over=answer.fair_p_over,
                    books_count=answer.books_count,
                    dispersion=answer.dispersion,
                )
            )
            result.written += 1
        self._session.commit()
        return result

    def _consensus_config(self, stat: str, position: str | None) -> ConsensusConfig:
        """The consensus settings with the stat's fitted curve for this player's position, or the
        fallback curve if it has none."""
        model = self._cfg.params.model(stat, position) if self._cfg.params else None
        return replace(
            self._cfg.consensus,
            line_cv=self._cfg.line_cv.get(stat, DEFAULT_LINE_CV),
            line_model=FittedLineModel(model) if model else None,
        )

    def _quotes(
        self, game_id: str, player_ids: Iterable[int] | None, as_of: datetime
    ) -> dict[tuple[str, int], list[BookQuote]]:
        """Each book's latest main-market quote for the game in the stale window before as_of."""
        columns = (PropSnapshot.market_key, PropSnapshot.player_id, PropSnapshot.book_key)
        stmt = (
            select(
                *columns,
                PropSnapshot.line,
                PropSnapshot.over_price,
                PropSnapshot.under_price,
                PropSnapshot.captured_at,
            )
            .join(Market, Market.key == PropSnapshot.market_key)
            .where(
                PropSnapshot.game_id == game_id,
                Market.is_alternate.is_(False),
                PropSnapshot.captured_at <= as_of,
                PropSnapshot.captured_at >= as_of - self._cfg.consensus.stale_after,
            )
            .ext(distinct_on(*columns))
            .order_by(*columns, PropSnapshot.captured_at.desc())
        )
        if player_ids is not None:
            stmt = stmt.where(PropSnapshot.player_id.in_(list(player_ids)))
        groups: dict[tuple[str, int], list[BookQuote]] = defaultdict(list)
        for row in self._session.execute(stmt):
            groups[(row.market_key, row.player_id)].append(
                BookQuote(row.book_key, row.line, row.over_price, row.under_price, row.captured_at)
            )
        return groups

    def _latest_consensus(
        self, game_id: str, as_of: datetime
    ) -> dict[tuple[str, int], ConsensusSnapshot]:
        columns = (ConsensusSnapshot.market_key, ConsensusSnapshot.player_id)
        stmt = (
            select(ConsensusSnapshot)
            .where(ConsensusSnapshot.game_id == game_id, ConsensusSnapshot.captured_at <= as_of)
            .ext(distinct_on(*columns))
            .order_by(*columns, ConsensusSnapshot.captured_at.desc())
        )
        return {(row.market_key, row.player_id): row for row in self._session.scalars(stmt)}


def _same(stored: ConsensusSnapshot | None, answer: Consensus) -> bool:
    if stored is None or stored.books_count != answer.books_count:
        return False
    return (
        _close(stored.fair_line, answer.fair_line)
        and _close(stored.fair_p_over, answer.fair_p_over)
        and _close(stored.dispersion, answer.dispersion)
    )


def _close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= TOLERANCE
