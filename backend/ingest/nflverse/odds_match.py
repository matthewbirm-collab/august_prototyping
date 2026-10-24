"""Link nflverse games to odds-provider events by teams and kickoff time."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Game, Team
from ingest.odds.provider import OddsEvent

logger = logging.getLogger(__name__)

KICKOFF_TOLERANCE = timedelta(hours=24)


@dataclass
class MatchReport:
    matched: dict[str, str] = field(default_factory=dict)
    """game id -> odds event id"""
    unmatched_events: list[OddsEvent] = field(default_factory=list)


def match_odds_events(session: Session, events: Sequence[OddsEvent]) -> MatchReport:
    """Set `games.odds_event_id` for each event whose teams and kickoff match a game.

    Teams match by full name (`teams.name`). Home and away may be swapped, since providers and
    nflverse can disagree about neutral-site games. Kickoffs must be within 24 hours. The caller
    commits.
    """
    abbr_by_name = {name: abbr for abbr, name in session.execute(select(Team.abbr, Team.name))}
    report = MatchReport()
    for event in events:
        home = abbr_by_name.get(event.home_team)
        away = abbr_by_name.get(event.away_team)
        game = None
        if home and away:
            window = (
                event.commence_time - KICKOFF_TOLERANCE,
                event.commence_time + KICKOFF_TOLERANCE,
            )
            game = session.scalars(
                select(Game)
                .where(
                    Game.kickoff_utc.between(*window),
                    ((Game.home_abbr == home) & (Game.away_abbr == away))
                    | ((Game.home_abbr == away) & (Game.away_abbr == home)),
                )
                .order_by(Game.kickoff_utc)
            ).first()
        if game is None:
            report.unmatched_events.append(event)
            logger.warning(
                "odds event has no matching game",
                extra={
                    "event_id": event.event_id,
                    "home_team": event.home_team,
                    "away_team": event.away_team,
                    "commence_time": event.commence_time.isoformat(),
                },
            )
            continue
        game.odds_event_id = event.event_id
        report.matched[game.id] = event.event_id
    return report
