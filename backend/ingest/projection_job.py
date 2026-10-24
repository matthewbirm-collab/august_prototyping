"""Turn the latest consensus into fantasy projections and write them to `projections`.

Runs after the consensus job on each capture (it is the second recompute hook), and can be called
for any moment in the past: it reads only consensus rows, quotes and finished games dated no later
than `as_of`, so a backtest sees what the live app saw.

One row per (player, scoring config) whose projection differs from the latest stored one. Rows
are never updated: `computed_at` is when the engine ran, `as_of` the moment it computed for.
"""

import logging
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.distribution_params import (
    DEFAULT_PATH,
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
from engine.distributions import StatModel, fit_anytime_td, fit_line
from engine.gapfill import (
    PlayerHistory,
    Shares,
    TeamVolume,
    distribution,
    fill,
    shares,
    team_volume,
)
from engine.projection import (
    ENGINE_VERSION,
    POSITION_STATS,
    FantasyProjection,
    StatInput,
    project,
)
from engine.scoring import Position, ScoringWeights
from ingest.recompute import RecomputeRequest
from ingest.usage import WINDOW_GAMES, load_usage, skill_position

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
    usage_window: int = WINDOW_GAMES
    """Team games behind the usage shares used to fill stats with no market."""
    use_markets: bool = True
    """False projects every stat from the gap-fill model: a backtest with no props."""

    @classmethod
    def from_settings(cls, settings: Settings) -> "ProjectionJobConfig":
        return cls(
            stale_after=timedelta(minutes=settings.stale_quote_minutes),
            params=load_distribution_params(settings.distribution_params_path or DEFAULT_PATH),
        )


@dataclass(frozen=True)
class ComputedProjection:
    player_id: int
    position: Position
    scoring_config_id: int
    projection: FantasyProjection


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

    def compute(
        self,
        game_id: str,
        player_ids: Iterable[int] | None,
        as_of: datetime,
        scoring_keys: Collection[str] | None = None,
    ) -> tuple[list[ComputedProjection], int]:
        """Projections for each player of the game as of `as_of`, without writing them: players
        with consensus, plus rostered players with recent usage and no props at all. Stats with
        no market are gap-filled. `player_ids` None means all of them; `scoring_keys` None means
        every scoring config. Returns the projections and the number of players skipped."""
        game = self._session.get(Game, game_id)
        configs = self._scoring_configs(scoring_keys)
        consensus = self._consensus(game_id, player_ids, as_of) if self._cfg.use_markets else {}
        newest = self._newest_quote(game_id, as_of)
        stats = dict(self._session.execute(select(Market.key, Market.stat)).all())
        usage = self._usage(game, as_of)
        if player_ids is not None:
            wanted = set(player_ids)
            usage = {pid: u for pid, u in usage.items() if pid in wanted}
        candidates = set(consensus) | {
            pid for pid, (_, _, player) in usage.items() if _has_usage(player)
        }
        positions = dict(
            self._session.execute(
                select(Player.id, Player.position).where(Player.id.in_(list(candidates)))
            ).all()
        )

        found: list[ComputedProjection] = []
        skipped = 0
        for player_id in sorted(candidates):
            position = skill_position(positions.get(player_id))
            if position is None and player_id in usage:
                position = usage[player_id][2].position
            if position is None:
                skipped += 1
                continue
            inputs = self._stat_inputs(
                position, consensus.get(player_id, []), stats, newest, player_id, as_of
            )
            if player_id in usage:
                inputs.update(self._filled(position, inputs, *usage[player_id]))
            if not inputs:
                skipped += 1
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
                found.append(ComputedProjection(player_id, position, config_id, answer))
        return found, skipped

    def recompute(
        self, game_id: str, player_ids: Iterable[int] | None, as_of: datetime
    ) -> ProjectionResult:
        """Compute as of `as_of` and write a row for each (player, scoring config) whose
        projection differs from the latest stored one. Commits."""
        found, skipped = self.compute(game_id, player_ids, as_of)
        result = ProjectionResult(skipped_players=skipped)
        computed_at = self._clock()
        for item in found:
            player_id, config_id, answer = item.player_id, item.scoring_config_id, item.projection
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

    # Gap filling ------------------------------------------------------------------------------

    def _usage(
        self, game: Game | None, as_of: datetime
    ) -> dict[int, tuple[TeamVolume, Shares, PlayerHistory]]:
        """player -> (his team's expected volume, his shares, his history), for both teams.
        Empty when the game has no spread and total or a team has no finished games yet."""
        if game is None or game.spread_line is None or game.total_line is None:
            return {}
        found: dict[int, tuple[TeamVolume, Shares, PlayerHistory]] = {}
        for team, team_usage in load_usage(
            self._session, game, as_of, self._cfg.usage_window
        ).items():
            # nflverse spreads are positive when the home team is favoured.
            spread = game.spread_line if team == game.home_abbr else -game.spread_line
            volume = team_volume(team_usage.history, game.total_line, spread)
            players = list(team_usage.players.values())
            for player_id, share in shares(team_usage.history, players).items():
                found[player_id] = (volume, share, team_usage.players[player_id])
        return found

    def _filled(
        self,
        position: Position,
        markets: Mapping[str, StatInput],
        volume: TeamVolume,
        share: Shares,
        player: PlayerHistory,
    ) -> dict[str, StatInput]:
        """Model inputs for the stats this player has no market for."""
        missing = set(POSITION_STATS[position]) - set(markets)
        filled: dict[str, StatInput] = {}
        for stat, mean in fill(replace(player, position=position), share, volume, missing).items():
            model = (
                StatModel("poisson")
                if stat == "anytime_td"
                else (self._cfg.params.model(stat, position) if self._cfg.params else None)
            )
            filled[stat] = StatInput(
                distribution=distribution(model or StatModel("poisson"), mean),
                books=0,
                source="model",
            )
        return filled

    # Inputs -----------------------------------------------------------------------------------

    def _scoring_configs(self, keys: Collection[str] | None = None) -> dict[int, ScoringWeights]:
        stmt = select(ScoringConfig).order_by(ScoringConfig.id)
        if keys is not None:
            stmt = stmt.where(ScoringConfig.key.in_(list(keys)))
        return {
            row.id: ScoringWeights.model_validate(row.weights)
            for row in self._session.scalars(stmt)
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


def _has_usage(player: PlayerHistory) -> bool:
    """Available, and averaging at least one target, carry or pass attempt in the games he
    played: enough to project a player who has no props."""
    if not player.available or player.games <= 0:
        return False
    return (player.targets + player.carries + player.pass_attempts) / player.games >= 1


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
            "modelled": list(c.modelled),
        },
    }
