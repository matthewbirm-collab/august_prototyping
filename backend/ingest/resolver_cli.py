"""Review sportsbook names the resolver couldn't match.

uv run python -m ingest.resolver_cli list [--season 2026 --week 8]
uv run python -m ingest.resolver_cli confirm <unmatched_id> <player_id>
uv run python -m ingest.resolver_cli ignore <unmatched_id>
uv run python -m ingest.resolver_cli rate --season 2026 --week 8
"""

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Game, Player, PlayerAlias, UnmatchedName
from db.session import get_engine
from ingest.names import normalize_name
from ingest.resolver import weekly_unmatched_rate
from ingest.rosters import DbRosterLookup


def list_unmatched(
    session: Session, out: TextIO, *, season: int | None = None, week: int | None = None
) -> int:
    stmt = (
        select(UnmatchedName, Game)
        .join(Game, Game.id == UnmatchedName.game_id)
        .where(UnmatchedName.status == "open")
        .order_by(UnmatchedName.occurrences.desc(), UnmatchedName.id)
    )
    if season is not None:
        stmt = stmt.where(Game.season == season)
    if week is not None:
        stmt = stmt.where(Game.week == week)
    rows = session.execute(stmt).all()
    if not rows:
        print("No open unmatched names.", file=out)
        return 0

    rosters = DbRosterLookup(session)
    for unmatched, game in rows:
        hint = f" [{unmatched.position_hint}]" if unmatched.position_hint else ""
        print(
            f"#{unmatched.id}  {unmatched.raw_name!r}{hint}  {game.id}  "
            f"seen {unmatched.occurrences}x",
            file=out,
        )
        candidates = rosters.players(game.season, game.week, [game.home_abbr, game.away_abbr])
        suggestions = process.extract(
            normalize_name(unmatched.raw_name),
            {c.player_id: normalize_name(c.full_name) for c in candidates},
            scorer=fuzz.token_sort_ratio,
            limit=3,
        )
        by_id = {c.player_id: c for c in candidates}
        for _, score, player_id in suggestions:
            c = by_id[player_id]
            print(
                f"    {player_id}: {c.full_name} ({c.position}, {c.team_abbr})  {score:.0f}",
                file=out,
            )
        if not suggestions:
            print("    no rostered players for this game yet", file=out)
    return 0


def confirm(session: Session, out: TextIO, unmatched_id: int, player_id: int) -> int:
    unmatched = session.get(UnmatchedName, unmatched_id)
    player = session.get(Player, player_id)
    if unmatched is None or player is None:
        missing = "unmatched name" if unmatched is None else "player"
        print(f"No such {missing}.", file=sys.stderr)
        return 1
    if (
        session.scalar(
            select(PlayerAlias.id).where(
                PlayerAlias.source == unmatched.source,
                PlayerAlias.raw_name == unmatched.raw_name,
                PlayerAlias.player_id == player_id,
            )
        )
        is None
    ):
        session.add(
            PlayerAlias(
                source=unmatched.source,
                raw_name=unmatched.raw_name,
                player_id=player_id,
                method="manual",
            )
        )
    unmatched.status = "confirmed"
    unmatched.resolved_player_id = player_id
    session.commit()
    print(f"{unmatched.raw_name!r} -> {player.full_name} ({player_id})", file=out)
    return 0


def ignore(session: Session, out: TextIO, unmatched_id: int) -> int:
    unmatched = session.get(UnmatchedName, unmatched_id)
    if unmatched is None:
        print("No such unmatched name.", file=sys.stderr)
        return 1
    unmatched.status = "ignored"
    session.commit()
    print(f"Ignored {unmatched.raw_name!r}.", file=out)
    return 0


def rate(session: Session, out: TextIO, season: int, week: int) -> int:
    result = weekly_unmatched_rate(session, season, week)
    print(
        f"{season} week {week}: {result.unmatched} unmatched, {result.matched_players} matched "
        f"players, unmatched rate {result.rate:.1%}",
        file=out,
    )
    return 0


def main(argv: Sequence[str] | None = None, session: Session | None = None) -> int:
    parser = argparse.ArgumentParser(prog="resolver_cli", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    list_cmd = commands.add_parser("list", help="open unmatched names with suggestions")
    list_cmd.add_argument("--season", type=int)
    list_cmd.add_argument("--week", type=int)

    confirm_cmd = commands.add_parser("confirm", help="map an unmatched name to a player")
    confirm_cmd.add_argument("unmatched_id", type=int)
    confirm_cmd.add_argument("player_id", type=int)

    ignore_cmd = commands.add_parser("ignore", help="stop showing an unmatched name")
    ignore_cmd.add_argument("unmatched_id", type=int)

    rate_cmd = commands.add_parser("rate", help="weekly unmatched rate")
    rate_cmd.add_argument("--season", type=int, required=True)
    rate_cmd.add_argument("--week", type=int, required=True)

    args = parser.parse_args(argv)
    owned = session is None
    db = session if session is not None else Session(get_engine())
    try:
        match args.command:
            case "list":
                return list_unmatched(db, sys.stdout, season=args.season, week=args.week)
            case "confirm":
                return confirm(db, sys.stdout, args.unmatched_id, args.player_id)
            case "ignore":
                return ignore(db, sys.stdout, args.unmatched_id)
            case "rate":
                return rate(db, sys.stdout, args.season, args.week)
        return 2
    finally:
        if owned:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
