"""The backtest replay: no leakage, grading, ECR baseline and the regression check."""

import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from analytics import backtest_cli
from analytics.backtest_cli import Backtest, default_moment
from analytics.ecr import EcrCurves
from db.models import ActualStats, ConsensusSnapshot, DepthChart, FfRanking, Game, Injury
from engine.projection import FantasyProjection
from tests.db import test_gapfill_job as gapfill_world
from tests.db.backfill_support import GAME_A, KICKOFF_A
from tests.db.test_consensus_job import AS_OF, KELCE, PACHECO, RUSH, TD, put

T1H = KICKOFF_A - timedelta(hours=1)
CURVES = EcrCurves(
    {"2025": {"ppr": {"RB": [18.0, 12.0, 8.0], "TE": [14.0, 9.0], "QB": [22.0, 18.0]}}}
)
HUNT, MAHOMES = gapfill_world.HUNT, gapfill_world.MAHOMES


@pytest.fixture
def db(session: Session) -> Iterator[Session]:
    yield gapfill_world.build_kc_world(session)


def minutes_before_as_of(moment: datetime) -> float:
    return (AS_OF - moment).total_seconds() / 60


def quote_before_t1h(db: Session) -> None:
    """Three books on Pacheco's rushing line and two on Kelce scoring, minutes before T-1h."""
    at = minutes_before_as_of(T1H - timedelta(minutes=5))
    for book, line in (("draftkings", 64.5), ("fanduel", 65.5), ("betmgm", 64.5)):
        put(db, book, RUSH, PACHECO, line, -110, -110, minutes_before=at)
    for book, price in (("draftkings", 120), ("fanduel", 110)):
        put(db, book, TD, KELCE, None, price, None, minutes_before=at)


def game_a(db: Session) -> Game:
    game = db.get(Game, GAME_A)
    assert game is not None
    return game


def snapshot(backtest: Backtest, db: Session, at: datetime) -> dict[int, FantasyProjection]:
    """Everything the replay projected for GAME_A as of `at`, by player."""
    return {player: p for player, _, p in backtest.project(game_a(db), at)}


def add_the_future(db: Session) -> None:
    """Everything a leaky replay could use: later quotes with a wild move, the game's own box
    score, an injury report, a depth-chart change and a later consensus row."""
    later = minutes_before_as_of(T1H + timedelta(minutes=30))
    for book in ("draftkings", "fanduel", "betmgm"):
        put(db, book, RUSH, PACHECO, 110.5, -110, -110, minutes_before=later)
    db.add(
        ActualStats(
            game_id=GAME_A,
            player_id=HUNT,
            season=2025,
            week=8,
            rush_attempts=25,
            rush_yds=180,
            rush_tds=3,
            fumbles_lost=2,
        )
    )
    db.add(
        Injury(
            season=2025,
            week=8,
            player_id=KELCE,
            available_at=T1H + timedelta(minutes=20),
            team_abbr="KC",
            report_status="Out",
        )
    )
    db.add(
        DepthChart(
            season=2025,
            week=8,
            player_id=HUNT,
            position="RB",
            available_at=T1H + timedelta(minutes=20),
            team_abbr="KC",
            depth_rank=1,
        )
    )
    db.add(
        ConsensusSnapshot(
            captured_at=T1H + timedelta(minutes=10),
            game_id=GAME_A,
            player_id=KELCE,
            market_key=TD,
            fair_line=None,
            fair_p_over=0.9,
            books_count=5,
            dispersion=0.0,
        )
    )
    db.commit()


def test_a_replay_uses_nothing_dated_after_its_moment(db: Session) -> None:
    quote_before_t1h(db)
    backtest = Backtest(db, "ppr", curves=CURVES)
    before = snapshot(backtest, db, T1H)
    assert set(before) >= {PACHECO, KELCE, HUNT}

    add_the_future(db)
    assert snapshot(backtest, db, T1H) == before

    # The later data is real: a replay at the close does see it.
    after = snapshot(backtest, db, KICKOFF_A - timedelta(minutes=1))
    assert after[PACHECO].mean > before[PACHECO].mean + 3
    assert after[KELCE].mean != before[KELCE].mean


def test_moments_default_to_offsets_from_kickoff() -> None:
    assert default_moment(KICKOFF_A, "t1h") == T1H
    assert default_moment(KICKOFF_A, "t24h") == KICKOFF_A - timedelta(hours=24)
    assert default_moment(KICKOFF_A, "close") == KICKOFF_A - timedelta(minutes=1)


def add_rankings(db: Session, scraped: date) -> None:
    for player, fantasypros_id, page, position, ecr in (
        (PACHECO, 101, "weekly-rb", "RB", 1.0),
        (HUNT, 102, "weekly-rb", "RB", 2.0),
        (KELCE, 103, "weekly-te", "TE", 1.0),
    ):
        db.add(
            FfRanking(
                season=2025,
                week=8,
                scrape_date=scraped,
                page_type=page,
                fantasypros_id=fantasypros_id,
                player_id=player,
                player_name=str(player),
                position=position,
                ecr=ecr,
            )
        )
    db.commit()


def box_scores(db: Session) -> None:
    for player, yards in ((PACHECO, 80), (HUNT, 20), (KELCE, 50), (MAHOMES, 0)):
        db.add(
            ActualStats(
                game_id=GAME_A,
                player_id=player,
                season=2025,
                week=8,
                rush_yds=yards if player in (PACHECO, HUNT) else 0,
                rec_yds=yards if player == KELCE else 0,
                receptions=5 if player == KELCE else 0,
                pass_yds=280 if player == MAHOMES else 0,
                pass_tds=2 if player == MAHOMES else 0,
            )
        )
    db.commit()


def test_games_are_graded_against_the_box_score_with_the_ecr_baseline(db: Session) -> None:
    quote_before_t1h(db)
    box_scores(db)
    add_rankings(db, date(2025, 10, 17))
    rows = Backtest(db, "ppr", curves=CURVES).run([2025], ["t1h"], [8])

    found = {r.player_id: r for r in rows if r.game_id == GAME_A}
    assert found[PACHECO].actual == pytest.approx(8.0)
    assert found[KELCE].actual == pytest.approx(5 + 5.0)
    assert found[MAHOMES].actual == pytest.approx(11.2 + 8)
    assert (found[PACHECO].ecr, found[HUNT].ecr, found[KELCE].ecr) == (18.0, 12.0, 14.0)
    assert found[MAHOMES].ecr is None
    assert found[HUNT].model_share == pytest.approx(1.0)
    assert 0 < found[PACHECO].model_share < 1


def test_a_ranking_scraped_on_game_day_is_not_used(db: Session) -> None:
    box_scores(db)
    add_rankings(db, KICKOFF_A.date())
    rows = Backtest(db, "ppr", curves=CURVES, model_only=True).run([2025], ["t1h"], [8])
    assert all(r.ecr is None for r in rows)


def test_model_only_ignores_the_markets(db: Session) -> None:
    quote_before_t1h(db)
    box_scores(db)
    rows = Backtest(db, "ppr", curves=CURVES, model_only=True).run([2025], ["t1h"], [8])
    assert {r.model_share for r in rows} == {1.0}


def test_the_cli_writes_reports_and_fails_on_a_regression(db: Session, tmp_path: Path) -> None:
    quote_before_t1h(db)
    box_scores(db)
    add_rankings(db, date(2025, 10, 17))
    out, baseline = tmp_path / "out", tmp_path / "baseline.json"
    args = ["--seasons", "2025", "--weeks", "8", "--snapshots", "t1h,close"]
    args += ["--out", str(out), "--baseline", str(baseline)]

    assert backtest_cli.main([*args, "--update-baseline"], session=db, curves=CURVES) == 0
    assert {p.name for p in out.iterdir()} == {
        "by_position.csv",
        "by_week.csv",
        "coverage.png",
        "summary.md",
    }
    assert "market/ppr" in baseline.read_text()
    assert backtest_cli.main(args, session=db, curves=CURVES) == 0

    # Pretend the last saved run was much better.
    stored = json.loads(baseline.read_text())
    stored["market/ppr"]["t1h"]["RB"] /= 2
    baseline.write_text(json.dumps(stored))
    assert backtest_cli.main(args, session=db, curves=CURVES) == 1


def test_nothing_to_grade_is_an_error(db: Session, tmp_path: Path) -> None:
    args = ["--seasons", "2019", "--out", str(tmp_path), "--baseline", str(tmp_path / "b.json")]
    assert backtest_cli.main(args, session=db, curves=CURVES) == 2
    with pytest.raises(SystemExit):
        backtest_cli.main(["--seasons", "2025", "--snapshots", "t3h"], session=db)
