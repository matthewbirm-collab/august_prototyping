from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.settings import get_settings
from db.models import ActualStats, ConsensusSnapshot, Game, Projection, ScoringConfig
from engine.distributions import fit_line
from engine.projection import ENGINE_VERSION, StatInput, project
from engine.scoring import standard_weights
from ingest import run_ingest
from ingest.consensus_job import ConsensusJob
from ingest.projection_job import ProjectionJob, ProjectionJobConfig
from ingest.recompute import RecomputeRequest, chain
from tests.db import backfill_support as world
from tests.db import support
from tests.db.backfill_support import GAME_A, KICKOFF_A
from tests.db.test_consensus_job import AS_OF, KELCE, PACHECO, RUSH, TD, put, rush_quotes

COMPUTED = datetime(2025, 10, 19, 12, 0, 30, tzinfo=UTC)


@pytest.fixture
def db(session: Session) -> Iterator[Session]:
    yield world.build_world(session)


def quote_everything(db: Session) -> None:
    rush_quotes(db)
    put(db, "draftkings", TD, KELCE, None, 120, None)
    put(db, "fanduel", TD, KELCE, None, 110, None)
    ConsensusJob(db).recompute(GAME_A, None, AS_OF)


def job(db: Session) -> ProjectionJob:
    return ProjectionJob(db, shipped_config(), lambda: COMPUTED)


def shipped_config() -> ProjectionJobConfig:
    return ProjectionJobConfig.from_settings(get_settings())


def projections(db: Session, player: int | None = None) -> list[Projection]:
    stmt = select(Projection).order_by(Projection.player_id, Projection.scoring_config_id)
    if player is not None:
        stmt = stmt.where(Projection.player_id == player)
    return list(db.scalars(stmt))


def config_id(db: Session, key: str) -> int:
    return db.scalars(select(ScoringConfig.id).where(ScoringConfig.key == key)).one()


def for_config(db: Session, player: int, key: str) -> Projection:
    return next(r for r in projections(db, player) if r.scoring_config_id == config_id(db, key))


def earlier_games(db: Session, fumbles: list[int], player: int = PACHECO) -> None:
    """One finished game per entry, a week apart before the kickoff, with that many fumbles lost."""
    for week, lost in enumerate(fumbles, start=1):
        game_id = f"2025_{week:02d}_KC_LV"
        db.add(
            Game(
                id=game_id,
                season=2025,
                week=week,
                season_type="REG",
                kickoff_utc=KICKOFF_A - timedelta(days=7 * (len(fumbles) + 1 - week)),
                home_abbr="LV",
                away_abbr="KC",
            )
        )
        db.flush()
        db.add(
            ActualStats(
                game_id=game_id, player_id=player, season=2025, week=week, fumbles_lost=lost
            )
        )
    db.commit()


# What gets written -----------------------------------------------------------------------------


def test_one_row_per_player_and_scoring_config_matching_the_engine(db: Session) -> None:
    quote_everything(db)
    result = job(db).recompute(GAME_A, None, AS_OF)

    assert (result.written, result.unchanged, result.skipped_players) == (6, 0, 0)
    rows = projections(db)
    assert len(rows) == 6
    assert {(r.as_of, r.computed_at, r.engine_version) for r in rows} == {
        (AS_OF, COMPUTED, ENGINE_VERSION)
    }
    assert len({r.scoring_config_id for r in rows}) == 3

    ppr = for_config(db, KELCE, "ppr")
    stats = ppr.breakdown["stats"]
    assert set(stats) == {"anytime_td", "fumbles_lost"}
    assert stats["anytime_td"]["source"] == "market"
    assert stats["anytime_td"]["books"] == 2
    assert ppr.mean == pytest.approx(sum(s["points"] for s in stats.values()))
    assert ppr.p20 < ppr.mean < ppr.p80
    assert 0 < ppr.confidence < 1  # a tight end with only a touchdown market


def test_the_projection_equals_a_direct_engine_call_on_the_consensus(db: Session) -> None:
    quote_everything(db)
    job(db).recompute(GAME_A, None, AS_OF)

    consensus = db.scalars(
        select(ConsensusSnapshot).where(ConsensusSnapshot.player_id == PACHECO)
    ).one()
    shaped = shipped_config().params
    assert shaped is not None
    model = shaped.model("rush_yds", "RB")
    assert model is not None and consensus.fair_line is not None
    expected = project(
        "RB",
        standard_weights(1.0),
        {
            "rush_yds": StatInput(
                fit_line(model, consensus.fair_line, consensus.fair_p_over),
                consensus.books_count,
                consensus.dispersion,
                consensus.fair_line,
                2.0,
            )
        },
        (0 + 6 * 0.05) / 6,
    )
    row = for_config(db, PACHECO, "ppr")
    assert row.mean == pytest.approx(expected.mean)
    assert row.p20 == pytest.approx(expected.p20)
    assert row.p80 == pytest.approx(expected.p80)
    assert row.confidence == pytest.approx(expected.confidence.score)


def test_a_rerun_with_nothing_new_writes_nothing(db: Session) -> None:
    quote_everything(db)
    job(db).recompute(GAME_A, None, AS_OF)
    result = job(db).recompute(GAME_A, None, AS_OF)
    assert (result.written, result.unchanged) == (0, 6)
    assert len(projections(db)) == 6


def test_a_changed_consensus_adds_a_version_and_keeps_the_old_one(db: Session) -> None:
    quote_everything(db)
    job(db).recompute(GAME_A, None, AS_OF)

    later = AS_OF + timedelta(minutes=10)
    for book in ("draftkings", "fanduel", "betmgm"):
        put(db, book, RUSH, PACHECO, 74.5, -110, -110, minutes_before=-8)
    ConsensusJob(db).recompute(GAME_A, [PACHECO], later)
    result = job(db).recompute(GAME_A, [PACHECO], later)

    assert (result.written, result.unchanged) == (3, 0)
    pacheco = projections(db, PACHECO)
    assert len(pacheco) == 6
    ppr = config_id(db, "ppr")
    old, new = sorted((r for r in pacheco if r.scoring_config_id == ppr), key=lambda r: r.as_of)
    assert (old.as_of, new.as_of) == (AS_OF, later)
    assert new.mean > old.mean


def test_only_the_requested_players_are_projected(db: Session) -> None:
    quote_everything(db)
    result = job(db).recompute(GAME_A, [KELCE], AS_OF)
    assert result.written == 3
    assert {r.player_id for r in projections(db)} == {KELCE}


def test_a_custom_scoring_config_in_the_table_gets_its_own_rows(db: Session) -> None:
    premium = standard_weights(1.0).model_copy(
        update={"position_overrides": {"TE": {"reception": 1.5}}, "pass_td": 6.0}
    )
    db.add(ScoringConfig(key="te_premium", name="TE premium", weights=premium.model_dump()))
    db.commit()
    quote_everything(db)
    job(db).recompute(GAME_A, None, AS_OF)
    assert len(projections(db, KELCE)) == 4


# No leakage, fumbles, stale data ------------------------------------------------------------------


def test_fumble_rate_is_blended_with_the_league_rate_and_ignores_the_future(db: Session) -> None:
    quote_everything(db)
    earlier_games(db, [1, 0, 1])  # three finished games before kickoff, two fumbles lost
    # A game after as_of must not count.
    db.add(
        Game(
            id="2025_09_KC_LV",
            season=2025,
            week=9,
            season_type="REG",
            kickoff_utc=KICKOFF_A + timedelta(days=7),
            home_abbr="LV",
            away_abbr="KC",
        )
    )
    db.flush()
    db.add(
        ActualStats(game_id="2025_09_KC_LV", player_id=PACHECO, season=2025, week=9, fumbles_lost=5)
    )
    db.commit()

    job(db).recompute(GAME_A, [PACHECO], AS_OF)
    row = for_config(db, PACHECO, "ppr")
    rate = row.breakdown["stats"]["fumbles_lost"]["mean"]
    assert rate == pytest.approx((2 + 6 * 0.05) / (3 + 6))


def test_a_rookie_with_no_games_gets_the_league_rate(db: Session) -> None:
    quote_everything(db)
    job(db).recompute(GAME_A, [PACHECO], AS_OF)
    row = projections(db, PACHECO)[0]
    assert row.breakdown["stats"]["fumbles_lost"]["mean"] == pytest.approx(0.05)


def test_old_consensus_with_no_recent_quotes_is_not_projected(db: Session) -> None:
    quote_everything(db)
    result = job(db).recompute(GAME_A, None, AS_OF + timedelta(hours=3))
    assert (result.written, result.skipped_players) == (0, 2)
    assert projections(db) == []


def test_a_projection_as_of_an_earlier_moment_ignores_later_consensus(db: Session) -> None:
    quote_everything(db)
    result = job(db).recompute(GAME_A, None, AS_OF - timedelta(minutes=30))
    assert (result.written, result.skipped_players) == (0, 0)
    assert projections(db) == []


# Wiring ---------------------------------------------------------------------------------------


def test_it_runs_as_a_recompute_hook_after_consensus(db: Session) -> None:
    rush_quotes(db)
    hook = chain(ConsensusJob(db), job(db))
    hook(RecomputeRequest(GAME_A, (PACHECO,), AS_OF))
    assert {r.player_id for r in projections(db)} == {PACHECO}
    assert len(projections(db)) == 3


@pytest.fixture
def capture_world(capture_db: Session) -> Session:
    return capture_db


def test_run_ingest_writes_projections_after_every_run(capture_world: Session) -> None:
    db = capture_world
    odds = support.FakeOdds(support.standard_responses())
    assert run_ingest.main([], session=db, provider=odds, clock=lambda: support.NOW) == 0
    assert (db.scalar(select(func.count()).select_from(Projection)) or 0) > 0
