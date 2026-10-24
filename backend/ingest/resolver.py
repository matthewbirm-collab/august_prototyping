"""Map sportsbook player names to canonical player ids.

Matching order, always restricted to the two teams' rosters for that week:

1. a saved alias for this exact raw name
2. the normalized name (see `ingest.names`)
3. a fuzzy match (rapidfuzz) scoring at least `FUZZY_MATCH_THRESHOLD`, clearly ahead of the
   runner-up
4. otherwise the name is recorded in `unmatched_names` for review

The resolver never guesses: when two candidates tie at a step, the position hint breaks the
tie, and if it can't, the name goes unmatched. Every match is saved as a `player_alias`.
Callers own the transaction and commit.
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz, process
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from db.models import Game, PlayerAlias, PropSnapshot, UnmatchedName
from ingest.names import normalize_name
from ingest.rosters import RosterEntry, RosterLookup

Method = Literal["alias", "normalized", "fuzzy", "unmatched"]

# How far the best fuzzy score must lead the runner-up for the match to count.
FUZZY_MARGIN = 3.0

# Positions that plausibly carry each market, used only to break ties.
_PASSING = frozenset({"QB"})
_RECEIVING = frozenset({"WR", "TE", "RB"})
_RUSHING = frozenset({"RB", "QB", "WR"})
_MARKET_POSITIONS: dict[str, frozenset[str]] = {
    "player_pass_yds": _PASSING,
    "player_pass_tds": _PASSING,
    "player_pass_completions": _PASSING,
    "player_pass_interceptions": _PASSING,
    "player_rush_yds": _RUSHING,
    "player_rush_attempts": _RUSHING,
    "player_receptions": _RECEIVING,
    "player_reception_yds": _RECEIVING,
}


def position_hint_for_market(market_key: str) -> frozenset[str] | None:
    """Positions likely to carry a market, or None when any position could (anytime TD)."""
    return _MARKET_POSITIONS.get(market_key.removesuffix("_alternate"))


@dataclass(frozen=True)
class GameContext:
    game_id: str
    season: int
    week: int
    home_abbr: str
    away_abbr: str

    @classmethod
    def from_game(cls, game: Game) -> "GameContext":
        return cls(game.id, game.season, game.week, game.home_abbr, game.away_abbr)


@dataclass(frozen=True)
class Resolution:
    player_id: int | None
    method: Method
    score: float | None = None


class GameResolver:
    """Resolves names for one game; rosters load once and results are cached per name."""

    def __init__(
        self,
        session: Session,
        game: GameContext,
        candidates: list[RosterEntry],
        *,
        source: str,
        fuzzy_threshold: float,
    ) -> None:
        self._session = session
        self._game = game
        self._candidates = candidates
        self._source = source
        self._threshold = fuzzy_threshold
        self._normalized = {c.player_id: normalize_name(c.full_name) for c in candidates}
        self._cache: dict[tuple[str, frozenset[str] | None, str | None], Resolution] = {}

    def resolve(
        self,
        raw_name: str,
        *,
        position_hint: Collection[str] | None = None,
        team_hint: str | None = None,
    ) -> Resolution:
        hint = frozenset(position_hint) if position_hint else None
        key = (raw_name, hint, team_hint)
        if key not in self._cache:
            self._cache[key] = self._resolve(raw_name, hint, team_hint)
        return self._cache[key]

    def _resolve(
        self, raw_name: str, hint: frozenset[str] | None, team_hint: str | None
    ) -> Resolution:
        pool = [c for c in self._candidates if team_hint is None or c.team_abbr == team_hint]
        pool_ids = {c.player_id for c in pool}

        alias_ids = set(
            self._session.scalars(
                select(PlayerAlias.player_id).where(
                    PlayerAlias.source == self._source, PlayerAlias.raw_name == raw_name
                )
            )
        )
        alias_match = _pick([c for c in pool if c.player_id in alias_ids], hint)
        if alias_match is not None:
            return Resolution(alias_match.player_id, "alias")

        target = normalize_name(raw_name)
        exact = [c for c in pool if self._normalized[c.player_id] == target]
        if exact:
            # A tie here would tie under fuzzy matching too, so don't fall through.
            match = _pick(exact, hint)
            if match is None:
                return self._unmatched(raw_name, hint, team_hint)
            return self._matched(raw_name, match, "normalized", 100.0)

        scored = process.extract(
            target,
            {pid: name for pid, name in self._normalized.items() if pid in pool_ids},
            scorer=fuzz.token_sort_ratio,
            score_cutoff=self._threshold,
            limit=None,
        )
        by_id = {c.player_id: c for c in pool}
        ranked = [(by_id[pid], score) for _, score, pid in scored]
        if hint and any(c.position in hint for c, _ in ranked):
            ranked = [(c, s) for c, s in ranked if c.position in hint]
        if not ranked:
            return self._unmatched(raw_name, hint, team_hint)
        best, best_score = ranked[0]
        if len(ranked) > 1 and best_score - ranked[1][1] < FUZZY_MARGIN:
            return self._unmatched(raw_name, hint, team_hint)
        return self._matched(raw_name, best, "fuzzy", best_score)

    def _matched(
        self, raw_name: str, candidate: RosterEntry, method: Method, score: float
    ) -> Resolution:
        self._session.execute(
            insert(PlayerAlias)
            .values(
                source=self._source,
                raw_name=raw_name,
                player_id=candidate.player_id,
                method=method,
                score=score,
            )
            .on_conflict_do_nothing()
        )
        return Resolution(candidate.player_id, method, score)

    def _unmatched(
        self, raw_name: str, hint: frozenset[str] | None, team_hint: str | None
    ) -> Resolution:
        stmt = insert(UnmatchedName).values(
            source=self._source,
            raw_name=raw_name,
            game_id=self._game.game_id,
            team_hint=team_hint,
            position_hint=",".join(sorted(hint)) if hint else None,
        )
        self._session.execute(
            stmt.on_conflict_do_update(
                index_elements=["source", "raw_name", "game_id"],
                set_={
                    "occurrences": UnmatchedName.occurrences + 1,
                    "last_seen": func.now(),
                    "team_hint": stmt.excluded.team_hint,
                    "position_hint": stmt.excluded.position_hint,
                },
            )
        )
        return Resolution(None, "unmatched")


class PlayerResolver:
    def __init__(
        self,
        session: Session,
        rosters: RosterLookup,
        *,
        source: str,
        fuzzy_threshold: float,
    ) -> None:
        self._session = session
        self._rosters = rosters
        self._source = source
        self._threshold = fuzzy_threshold

    def for_game(self, game: GameContext) -> GameResolver:
        candidates = self._rosters.players(game.season, game.week, [game.home_abbr, game.away_abbr])
        return GameResolver(
            self._session,
            game,
            candidates,
            source=self._source,
            fuzzy_threshold=self._threshold,
        )


def _pick(matches: list[RosterEntry], hint: frozenset[str] | None) -> RosterEntry | None:
    """The single match, or the single one fitting the position hint; otherwise None."""
    if len(matches) == 1:
        return matches[0]
    if hint:
        fitting = [m for m in matches if m.position in hint]
        if len(fitting) == 1:
            return fitting[0]
    return None


@dataclass(frozen=True)
class UnmatchedRate:
    unmatched: int
    matched_players: int

    @property
    def rate(self) -> float:
        total = self.unmatched + self.matched_players
        return self.unmatched / total if total else 0.0


def weekly_unmatched_rate(session: Session, season: int, week: int) -> UnmatchedRate:
    """Open unmatched names / (distinct players with snapshots + open unmatched names)."""
    week_games = select(Game.id).where(Game.season == season, Game.week == week)
    unmatched = session.scalar(
        select(func.count())
        .select_from(UnmatchedName)
        .where(UnmatchedName.status == "open", UnmatchedName.game_id.in_(week_games))
    )
    matched = session.scalar(
        select(func.count(func.distinct(PropSnapshot.player_id))).where(
            PropSnapshot.game_id.in_(week_games)
        )
    )
    return UnmatchedRate(unmatched=unmatched or 0, matched_players=matched or 0)


def season_unmatched_rate(session: Session, season: int) -> UnmatchedRate:
    """Distinct open unmatched names / (distinct players with snapshots + those names) for a season.

    Counted by name, not by game, so one unresolved player in many games counts once.
    """
    season_games = select(Game.id).where(Game.season == season)
    unmatched = session.scalar(
        select(func.count(func.distinct(UnmatchedName.raw_name))).where(
            UnmatchedName.status == "open", UnmatchedName.game_id.in_(season_games)
        )
    )
    matched = session.scalar(
        select(func.count(func.distinct(PropSnapshot.player_id))).where(
            PropSnapshot.game_id.in_(season_games)
        )
    )
    return UnmatchedRate(unmatched=unmatched or 0, matched_players=matched or 0)
