"""The projection job filling stats that have no market, from team volume and usage."""

from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.settings import get_settings
from db.models import ActualStats, Game, Injury, Player, Projection, Roster, ScoringConfig
from ingest.projection_job import ProjectionJob, ProjectionJobConfig
from tests.db import backfill_support as world
from tests.db.backfill_support import GAME_A, KICKOFF_A
from tests.db.test_consensus_job import AS_OF, KELCE, PACHECO, put, rush_quotes
from tests.db.test_projection_job import COMPUTED

HUNT, MAHOMES = 5, 6
# A week's line for each player: (targets, receptions, rec_yds, carries, rush_yds, rush_tds,
# rec_tds, pass attempts, pass yards, pass tds)
WEEK = {
    PACHECO: (3, 2, 15, 16, 70, 1, 0, 0, 0, 0),
    HUNT: (2, 2, 10, 6, 22, 0, 0, 0, 0, 0),
    KELCE: (8, 6, 60, 0, 0, 0, 1, 0, 0, 0),
    MAHOMES: (0, 0, 0, 4, 20, 0, 0, 36, 260, 2),
}


@pytest.fixture
def db(session: Session) -> Iterator[Session]:
    db = world.build_world(session)
    for player_id, name in ((HUNT, "Kareem Hunt"), (MAHOMES, "Patrick Mahomes")):
        db.add(Player(id=player_id, full_name=name, position="RB" if player_id == HUNT else "QB"))
    db.flush()
    for player_id in (HUNT, MAHOMES):
        db.add(
            Roster(
                season=2025,
                week=8,
                player_id=player_id,
                team_abbr="KC",
                position="RB" if player_id == HUNT else "QB",
                status="ACT",
                available_at=KICKOFF_A - timedelta(days=5),
            )
        )
    db.execute(update(Game).where(Game.id == GAME_A).values(spread_line=-6.5, total_line=46.5))
    four_earlier_weeks(db)
    db.commit()
    yield db


def four_earlier_weeks(db: Session) -> None:
    positions = {PACHECO: "RB", HUNT: "RB", KELCE: "TE", MAHOMES: "QB"}
    for week in range(4, 8):
        game_id = f"2025_{week:02d}_KC_DEN"
        db.add(
            Game(
                id=game_id,
                season=2025,
                week=week,
                season_type="REG",
                kickoff_utc=KICKOFF_A - timedelta(days=7 * (8 - week)),
                home_abbr="DEN",
                away_abbr="KC",
                spread_line=-3.0,
                total_line=44.0,
            )
        )
        db.flush()
        for player_id, line in WEEK.items():
            db.add(
                Roster(
                    season=2025,
                    week=week,
                    player_id=player_id,
                    team_abbr="KC",
                    position=positions[player_id],
                    status="ACT",
                    available_at=KICKOFF_A - timedelta(days=7 * (8 - week) + 5),
                )
            )
            tg, rec, ryds, car, rush, rtd, rectd, att, pyds, ptd = line
            db.add(
                ActualStats(
                    game_id=game_id,
                    player_id=player_id,
                    season=2025,
                    week=week,
                    targets=tg,
                    receptions=rec,
                    rec_yds=ryds,
                    rush_attempts=car,
                    rush_yds=rush,
                    rush_tds=rtd,
                    rec_tds=rectd,
                    pass_attempts=att,
                    completions=att * 2 // 3,
                    pass_yds=pyds,
                    pass_tds=ptd,
                )
            )


def job(db: Session) -> ProjectionJob:
    return ProjectionJob(db, ProjectionJobConfig.from_settings(get_settings()), lambda: COMPUTED)


def ppr(db: Session, player: int) -> Projection | None:
    config = db.scalars(select(ScoringConfig.id).where(ScoringConfig.key == "ppr")).one()
    return db.scalars(
        select(Projection)
        .where(Projection.player_id == player, Projection.scoring_config_id == config)
        .order_by(Projection.as_of.desc())
    ).first()


def consensus(db: Session) -> None:
    from ingest.consensus_job import ConsensusJob

    rush_quotes(db)
    ConsensusJob(db).recompute(GAME_A, None, AS_OF)


def test_a_backup_with_no_props_is_projected_from_usage(db: Session) -> None:
    consensus(db)
    job(db).recompute(GAME_A, None, AS_OF)

    hunt = ppr(db, HUNT)
    assert hunt is not None
    stats = hunt.breakdown["stats"]
    assert {stats[s]["source"] for s in ("rush_yds", "rec_yds", "receptions", "anytime_td")} == {
        "model"
    }
    assert stats["fumbles_lost"]["source"] == "history"
    assert 0 < hunt.confidence <= 0.4 + 1e-9
    assert hunt.breakdown["confidence"]["modelled"] == [
        "anytime_td",
        "rec_yds",
        "receptions",
        "rush_yds",
    ]
    # A quarter of the backfield's carries at a bit under four yards a carry, with KC favoured.
    assert 15 < stats["rush_yds"]["mean"] < 35
    assert 1 < hunt.mean < 12


def test_a_stat_with_a_market_keeps_it_and_the_rest_are_filled(db: Session) -> None:
    consensus(db)
    job(db).recompute(GAME_A, None, AS_OF)
    pacheco = ppr(db, PACHECO)
    assert pacheco is not None
    stats = pacheco.breakdown["stats"]
    assert stats["rush_yds"]["source"] == "market"
    assert stats["rush_yds"]["books"] == 3
    assert {stats[s]["source"] for s in ("rec_yds", "receptions", "anytime_td")} == {"model"}
    assert pacheco.confidence > ppr_confidence_of(db, HUNT)


def ppr_confidence_of(db: Session, player: int) -> float:
    row = ppr(db, player)
    assert row is not None
    return row.confidence


def test_a_quarterback_without_props_gets_passing_and_rushing_filled(db: Session) -> None:
    job(db).recompute(GAME_A, None, AS_OF)
    mahomes = ppr(db, MAHOMES)
    assert mahomes is not None
    stats = mahomes.breakdown["stats"]
    assert 200 < stats["pass_yds"]["mean"] < 320
    assert stats["anytime_td"]["mean"] < 0.3  # rushing scores only


def test_without_game_lines_nothing_is_filled(db: Session) -> None:
    db.execute(update(Game).where(Game.id == GAME_A).values(spread_line=None, total_line=None))
    db.commit()
    consensus(db)
    job(db).recompute(GAME_A, None, AS_OF)
    assert ppr(db, HUNT) is None
    pacheco = ppr(db, PACHECO)
    assert pacheco is not None
    assert set(pacheco.breakdown["stats"]) == {"rush_yds", "fumbles_lost"}


def test_a_starter_ruled_out_moves_his_carries_to_the_backup(db: Session) -> None:
    job(db).recompute(GAME_A, None, AS_OF)
    before = ppr(db, HUNT)
    db.add(
        Injury(
            season=2025,
            week=8,
            player_id=PACHECO,
            available_at=AS_OF - timedelta(hours=1),
            team_abbr="KC",
            report_status="Out",
        )
    )
    db.commit()
    job(db).recompute(GAME_A, None, AS_OF + timedelta(minutes=1))
    after = ppr(db, HUNT)
    assert before is not None and after is not None
    assert (
        after.breakdown["stats"]["rush_yds"]["mean"]
        > 2 * (before.breakdown["stats"]["rush_yds"]["mean"])
    )
    # Ruled out with no props: not projected at all.
    later = db.scalars(
        select(Projection).where(Projection.player_id == PACHECO, Projection.as_of > AS_OF)
    ).all()
    assert later == []


def test_an_injury_report_after_as_of_is_ignored(db: Session) -> None:
    db.add(
        Injury(
            season=2025,
            week=8,
            player_id=PACHECO,
            available_at=AS_OF + timedelta(hours=1),
            team_abbr="KC",
            report_status="Out",
        )
    )
    db.commit()
    job(db).recompute(GAME_A, None, AS_OF)
    assert ppr(db, PACHECO) is not None


def test_games_after_as_of_do_not_feed_the_usage(db: Session) -> None:
    job(db).recompute(GAME_A, None, AS_OF)
    before = ppr(db, HUNT)
    # Hunt's huge game next week must not leak into this week's shares.
    db.add(
        Game(
            id="2025_09_KC_DEN",
            season=2025,
            week=9,
            season_type="REG",
            kickoff_utc=KICKOFF_A + timedelta(days=7),
            home_abbr="KC",
            away_abbr="DEN",
        )
    )
    db.flush()
    db.add(
        Roster(
            season=2025,
            week=9,
            player_id=HUNT,
            team_abbr="KC",
            position="RB",
            status="ACT",
            available_at=KICKOFF_A + timedelta(days=2),
        )
    )
    db.add(
        ActualStats(game_id="2025_09_KC_DEN", player_id=HUNT, season=2025, week=9, rush_attempts=30)
    )
    db.commit()
    result = job(db).recompute(GAME_A, [HUNT], AS_OF)
    assert result.written == 0
    after = ppr(db, HUNT)
    assert before is not None and after is not None
    assert after.mean == before.mean


def test_only_the_requested_players_are_filled(db: Session) -> None:
    job(db).recompute(GAME_A, [HUNT], AS_OF)
    assert {r.player_id for r in db.scalars(select(Projection))} == {HUNT}


def test_unavailable_players_without_props_are_not_projected(db: Session) -> None:
    db.execute(
        update(Roster).where(Roster.player_id == HUNT, Roster.week == 8).values(status="RES")
    )
    db.commit()
    put(db, "draftkings", "player_rush_yds", PACHECO, 64.5, -110, -110)
    job(db).recompute(GAME_A, None, AS_OF)
    assert ppr(db, HUNT) is None
