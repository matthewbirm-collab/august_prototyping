"""Turn the latest consensus into fantasy projections and write them to `projections`.

Runs after the consensus job on each capture (it is the second recompute hook), and can be called
for any moment in the past: it reads only consensus rows, quotes and finished games dated no later
than `as_of`, so a backtest sees what the live app saw.

One row per (player, scoring config) whose projection differs from the latest stored one. Rows
are never updated: `computed_at` is when the engine ran, `as_of` the moment it computed for.
"""

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.distribution_params import (
    DEFAULT_PATH,
    POSITION_ALIASES,
    DistributionParams,
    load_distribution_params,
)
from app.settings import Settings
from db.models import (
    ActualStats,
    ConsensusSnapshot,
    Game,
    Market,
    Player,
    Projection,
    PropSnapshot,
    ScoringConfig,
)
from engine.distributions import fit_anytime_td, fit_line
from engine.projection import (
    ENGINE_VERSION,
    POSITION_STATS,
    FantasyProjection,
    StatInput,
    project,
)
from engine.scoring import Position, ScoringWeights
from ingest.recompute import RecomputeRequest

logger = logging.getLogger(__name__)

TOLERANCE = 1e-6
TRAILING_GAMES = 17
PRIOR_GAMES = 6
"""Games of league-average fumbling blended into a player's own rate, so a rookie's one fumble
doesn't read as a trend."""
LEAGUE_FUMBLES_LOST: Mapping[str, float] = {"QB": 0.14, "RB": 0.05, "WR": 0.02, "TE": 0.03}
"""Fumbles lost per game, a starting point to be replaced by a measured rate."""


@dataclass(frozen=True)
class ProjectionJobConfig:
    stale_after: timedelta = timedelta(minutes=90)
    params: DistributionParams | None = None
    league_fumbles: Mapping[str, float] = field(default_factory=lambda: dict(LEAGUE_FUMBLES_LOST))
    prior_games: int = PRIOR_GAMES
    trailing_games: int = TRAILING_GAMES

    @classmethod
    def from_settings(cls, settings: Settings) -> "ProjectionJobConfig":
        return cls(
            stale_after=timedelta(minutes=settings.stale_quote_minutes),
            params=load_distribution_params(settings.distribution_params_path or DEFAULT_PATH),
        )


@dataclass
class ProjectionResult:
    written: int = 0
    unchanged: int = 0
    skipped_players: int = 0
    """Players with no usable market at all, or a position the engine doesn't project."""


class ProjectionJob:
    """Callable as a recompute hook."""

    def __init__(
        self,
        session: Session,
        config: ProjectionJobConfig | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session = session
        self._cfg = config or ProjectionJobConfig(params=load_distribution_params(DEFAULT_PATH))
        self._clock = clock

    def __call__(self, request: RecomputeRequest) -> None:
        result = self.recompute(request.game_id, request.player_ids, request.as_of)
        logger.info(
            "projections recomputed",
            extra={
                "game": request.game_id,
                "written": result.written,
                "unchanged": result.unchanged,
                "skipped": result.skipped_players,
            },
        )

    def recompute(
        self, game_id: str, player_ids: Iterable[int] | None, as_of: datetime
    ) -> ProjectionResult:
        """Project each player of the game with consensus as of `as_of`, under every scoring
        config. `player_ids` None means all of them. Commits."""
        result = ProjectionResult()
        configs = self._scoring_configs()
        consensus = self._consensus(game_id, player_ids, as_of)
        newest = self._newest_quote(game_id, as_of)
        stats = dict(self._session.execute(select(Market.key, Market.stat)).all())
        positions = dict(
            self._session.execute(
                select(Player.id, Player.position).where(Player.id.in_(list(consensus)))
            ).all()
        )
        computed_at = self._clock()

        for player_id, rows in sorted(consensus.items()):
            position = _position(positions.get(player_id))
            inputs = (
                self._stat_inputs(position, rows, stats, newest, player_id, as_of)
                if position
                else {}
            )
            if position is None or not inputs:
                result.skipped_players += 1
                continue
            fumbles = self._fumble_rate(player_id, position, as_of)
            for config_id, weights in configs.items():
                answer = project(
                    position,
                    weights,
                    inputs,
                    fumbles,
                    stale_minutes=self._cfg.stale_after.total_seconds() / 60,
                )
                previous = self._latest(game_id, player_id, config_id, as_of)
                if previous is not None and _same(previous, answer):
                    result.unchanged += 1
                    continue
                self._session.add(
                    Projection(
                        computed_at=computed_at,
                        as_of=as_of,
                        game_id=game_id,
                        player_id=player_id,
                        scoring_config_id=config_id,
                        mean=answer.mean,
                        p20=answer.p20,
                        p80=answer.p80,
                        breakdown=_breakdown_json(answer),
                        confidence=answer.confidence.score,
                        engine_version=ENGINE_VERSION,
                    )
                )
                result.written += 1
        self._session.commit()
        return result

    # Inputs -----------------------------------------------------------------------------------

    def _scoring_configs(self) -> dict[int, ScoringWeights]:
        return {
            row.id: ScoringWeights.model_validate(row.weights)
            for row in self._session.scalars(select(ScoringConfig).order_by(ScoringConfig.id))
        }

    def _consensus(
        self, game_id: str, player_ids: Iterable[int] | None, as_of: datetime
    ) -> dict[int, list[ConsensusSnapshot]]:
        """The latest consensus row per (player, market) as of `as_of`, grouped by player."""
        columns = (ConsensusSnapshot.player_id, ConsensusSnapshot.market_key)
        stmt = (
            select(ConsensusSnapshot)
            .where(ConsensusSnapshot.game_id == game_id, ConsensusSnapshot.captured_at <= as_of)
            .ext(distinct_on(*columns))
            .order_by(*columns, ConsensusSnapshot.captured_at.desc())
        )
        if player_ids is not None:
            stmt = stmt.where(ConsensusSnapshot.player_id.in_(list(player_ids)))
        grouped: dict[int, list[ConsensusSnapshot]] = defaultdict(list)
        for row in self._session.scalars(stmt):
            grouped[row.player_id].append(row)
        return grouped

    def _newest_quote(self, game_id: str, as_of: datetime) -> dict[tuple[int, str], datetime]:
        """When each player market was last quoted by any book, as of `as_of`."""
        stmt = (
            select(
                PropSnapshot.player_id, PropSnapshot.market_key, func.max(PropSnapshot.captured_at)
            )
            .where(PropSnapshot.game_id == game_id, PropSnapshot.captured_at <= as_of)
            .group_by(PropSnapshot.player_id, PropSnapshot.market_key)
        )
        return {(player, market): at for player, market, at in self._session.execute(stmt)}

    def _stat_inputs(
        self,
        position: Position,
        rows: list[ConsensusSnapshot],
        stats: Mapping[str, str],
        newest: Mapping[tuple[int, str], datetime],
        player_id: int,
        as_of: datetime,
    ) -> dict[str, StatInput]:
        """Fit each usable consensus row; a stat whose quotes have all gone stale is left out."""
        inputs: dict[str, StatInput] = {}
        for row in rows:
            stat = stats.get(row.market_key)
            if stat not in POSITION_STATS[position]:
                continue
            quoted = newest.get((player_id, row.market_key))
            if quoted is None or as_of - quoted > self._cfg.stale_after:
                continue
            try:
                if stat == "anytime_td":
                    fitted = fit_anytime_td(row.fair_p_over)
                else:
                    model = self._cfg.params.model(stat, position) if self._cfg.params else None
                    if model is None or row.fair_line is None:
                        continue
                    fitted = fit_line(model, row.fair_line, row.fair_p_over)
            except ValueError:
                logger.warning(
                    "could not fit stat",
                    extra={"player": player_id, "market": row.market_key, "line": row.fair_line},
                )
                continue
            inputs[stat] = StatInput(
                distribution=fitted,
                books=row.books_count,
                dispersion=row.dispersion,
                line=row.fair_line,
                age_minutes=(as_of - quoted).total_seconds() / 60,
            )
        return inputs

    def _fumble_rate(self, player_id: int, position: Position, as_of: datetime) -> float:
        """Fumbles lost per game over the player's last games before `as_of`, pulled toward the
        position's league rate while the sample is small."""
        recent = (
            select(ActualStats.fumbles_lost)
            .join(Game, Game.id == ActualStats.game_id)
            .where(ActualStats.player_id == player_id, Game.kickoff_utc < as_of)
            .order_by(Game.kickoff_utc.desc())
            .limit(self._cfg.trailing_games)
            .subquery()
        )
        games, fumbles = self._session.execute(
            select(func.count(), func.coalesce(func.sum(recent.c.fumbles_lost), 0))
        ).one()
        prior = self._cfg.league_fumbles[position]
        return float((fumbles + self._cfg.prior_games * prior) / (games + self._cfg.prior_games))

    def _latest(
        self, game_id: str, player_id: int, config_id: int, as_of: datetime
    ) -> Projection | None:
        return self._session.scalars(
            select(Projection)
            .where(
                Projection.game_id == game_id,
                Projection.player_id == player_id,
                Projection.scoring_config_id == config_id,
                Projection.as_of <= as_of,
            )
            .order_by(Projection.as_of.desc(), Projection.computed_at.desc())
            .limit(1)
        ).first()


def _position(raw: str | None) -> Position | None:
    position = POSITION_ALIASES.get(raw or "", raw)
    return position if position in POSITION_STATS else None


def _same(stored: Projection, answer: FantasyProjection) -> bool:
    return (
        stored.engine_version == ENGINE_VERSION
        and abs(stored.mean - answer.mean) <= TOLERANCE
        and abs(stored.p20 - answer.p20) <= TOLERANCE
        and abs(stored.p80 - answer.p80) <= TOLERANCE
        and abs(stored.confidence - answer.confidence.score) <= TOLERANCE
    )


def _breakdown_json(answer: FantasyProjection) -> dict[str, object]:
    c = answer.confidence
    return {
        "stats": {
            stat: {
                "mean": b.mean,
                "p20": b.p20,
                "p80": b.p80,
                "weight": b.weight,
                "points": b.points,
                "source": b.source,
                "books": b.books,
            }
            for stat, b in answer.breakdown.items()
        },
        "confidence": {
            "score": c.score,
            "missing_markets": c.missing_markets,
            "dispersion": c.dispersion,
            "books": c.books,
            "freshness": c.freshness,
            "missing": list(c.missing),
        },
    }
