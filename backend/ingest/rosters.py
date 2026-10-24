"""Who was on a team in a given week: the candidate pool for the player resolver.

`DbRosterLookup` reads the weekly `rosters` table. Until `/p2-nflverse` loads it from nflverse,
that table is only filled by tests, so the resolver finds no candidates against live data.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Player, Roster


@dataclass(frozen=True)
class RosterEntry:
    player_id: int
    full_name: str
    position: str
    team_abbr: str


class RosterLookup(Protocol):
    def players(self, season: int, week: int, teams: Sequence[str]) -> list[RosterEntry]:
        """Players on any of `teams` in that season and week."""
        ...


class DbRosterLookup:
    def __init__(self, session: Session) -> None:
        self._session = session

    def players(self, season: int, week: int, teams: Sequence[str]) -> list[RosterEntry]:
        stmt = (
            select(Roster.player_id, Player.full_name, Roster.position, Roster.team_abbr)
            .join(Player, Player.id == Roster.player_id)
            .where(Roster.season == season, Roster.week == week, Roster.team_abbr.in_(teams))
            .order_by(Roster.player_id)
        )
        return [
            RosterEntry(
                player_id=row.player_id,
                full_name=row.full_name,
                position=row.position,
                team_abbr=row.team_abbr,
            )
            for row in self._session.execute(stmt)
        ]
