import csv
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analytics import calibration_cli
from analytics.calibration import Observation
from app.distribution_params import load_distribution_params
from db.models import ActualStats, BackfillProgress, ConsensusSnapshot
from db.snapshots import SnapshotQuote, store_prop_snapshots
from tests.db.backfill_support import GAME_A, GAME_B, KICKOFF_A, build_world

CLOSE = KICKOFF_A - timedelta(minutes=1)
PACHECO, KELCE, LOVE = 1, 2, 3
SHAPES = load_distribution_params()


@pytest.fixture
def db(session: Session) -> Iterator[Session]:
    yield build_world(session)


def closing(
    db: Session, game: str = GAME_A, snapshot_at: object = CLOSE, status: str = "done"
) -> None:
    db.add(
        BackfillProgress(
            game_id=game,
            label="close",
            target_at=CLOSE,
            snapshot_at=snapshot_at,
            status=status,
        )
    )
    db.commit()


def quote(
    db: Session,
    book: str,
    market: str,
    player: int,
    line: float | None,
    over: int,
    under: int | None,
) -> None:
    store_prop_snapshots(
        db,
        [
            SnapshotQuote(
                CLOSE - timedelta(minutes=2),
                GAME_A,
                book,
                market,
                player,
                line,
                over,
                under,
                "historical",
            )
        ],
    )
    db.commit()


def stats(db: Session, player: int, **values: int) -> None:
    db.add(ActualStats(game_id=GAME_A, player_id=player, season=2025, week=8, **values))
    db.commit()


def rush_and_td_quotes(db: Session) -> None:
    for book, line in (("draftkings", 64.5), ("fanduel", 64.5), ("betmgm", 65.5)):
        quote(db, book, "player_rush_yds", PACHECO, line, -110, -110)
    quote(db, "draftkings", "player_anytime_td", KELCE, None, 120, None)
    quote(db, "fanduel", "player_anytime_td", KELCE, None, 110, None)


def consensus_rows(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(ConsensusSnapshot)) or 0


# The pipeline ----------------------------------------------------------------------------------


def test_recompute_then_report_compares_each_prop_with_the_real_stat_line(
    db: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rush_and_td_quotes(db)
    closing(db)
    stats(db, PACHECO, rush_yds=70, rush_attempts=15)
    stats(db, KELCE, rec_tds=1)

    code = calibration_cli.main(
        ["--seasons", "2025", "--recompute", "--out", str(tmp_path)], session=db, params=SHAPES
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "Consensus written for 2 player markets in 1 games." in out
    assert "2 props from 1 games (0 skipped" in out
    assert "player_rush_yds" in out and "player_anytime_td" in out
    assert consensus_rows(db) == 2

    with (tmp_path / "summary.csv").open() as handle:
        summary = {r["market"]: r for r in csv.DictReader(handle)}
    assert set(summary) == {"player_rush_yds", "player_anytime_td"}
    assert summary["player_rush_yds"]["n"] == "1"
    assert float(summary["player_rush_yds"]["base_rate"]) == 1.0  # 70 yards beat the 64.5 line
    assert float(summary["player_anytime_td"]["base_rate"]) == 1.0  # a receiving touchdown
    assert summary["player_rush_yds"]["verdict"] in {
        "ranges about right",
        "ranges too narrow",
        "ranges too wide",
    }
    assert (tmp_path / "player_rush_yds.png").read_bytes()[:4] == b"\x89PNG"


def test_a_prop_that_stayed_under_and_a_player_without_touchdowns(
    db: Session, tmp_path: Path
) -> None:
    rush_and_td_quotes(db)
    closing(db)
    stats(db, PACHECO, rush_yds=40)
    stats(db, KELCE, rush_tds=0, rec_tds=0)
    calibration_cli.main(
        ["--seasons", "2025", "--recompute", "--out", str(tmp_path)], session=db, params=SHAPES
    )
    with (tmp_path / "summary.csv").open() as handle:
        summary = {r["market"]: r for r in csv.DictReader(handle)}
    assert float(summary["player_rush_yds"]["base_rate"]) == 0.0
    assert float(summary["player_anytime_td"]["base_rate"]) == 0.0


def test_a_rushing_touchdown_also_counts_for_anytime_td(db: Session) -> None:
    rush_and_td_quotes(db)
    closing(db)
    stats(db, KELCE, rush_tds=1, rec_tds=0)
    calibration_cli.recompute(db, calibration_cli.closing_snapshots(db, [2025], "close"))

    collected = calibration_cli.collect(db, [2025], "close", SHAPES)
    [touchdown] = [o for o in collected.observations if o.market == "player_anytime_td"]
    assert (touchdown.line, touchdown.actual) == (None, 1.0)


def test_players_without_a_stat_line_are_skipped_and_counted(
    db: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rush_and_td_quotes(db)
    closing(db)
    stats(db, PACHECO, rush_yds=70)  # Kelce never appears: inactive, so his prop is void

    calibration_cli.main(
        ["--seasons", "2025", "--recompute", "--out", str(tmp_path)], session=db, params=SHAPES
    )
    assert "1 props from 1 games (1 skipped: player has no stat line)" in capsys.readouterr().out


def test_observations_carry_the_fitted_shape_for_the_players_position(db: Session) -> None:
    rush_and_td_quotes(db)
    closing(db)
    stats(db, PACHECO, rush_yds=70)
    calibration_cli.recompute(db, calibration_cli.closing_snapshots(db, [2025], "close"))
    [rush] = [o for o in calibration_cli.collect(db, [2025], "close", SHAPES).observations]

    assert isinstance(rush, Observation)
    assert rush.model == SHAPES.model("rush_yds", "RB")
    assert rush.line == 64.5


# What is left out ------------------------------------------------------------------------------


def test_without_a_closing_consensus_there_is_nothing_to_report(
    db: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rush_and_td_quotes(db)
    closing(db)
    stats(db, PACHECO, rush_yds=70)
    code = calibration_cli.main(
        ["--seasons", "2025", "--out", str(tmp_path)], session=db, params=SHAPES
    )

    assert code == 1
    assert "run the historical backfill" in capsys.readouterr().err
    assert not (tmp_path / "summary.csv").exists()


def test_games_without_a_finished_closing_snapshot_are_ignored(db: Session, tmp_path: Path) -> None:
    rush_and_td_quotes(db)
    closing(db, snapshot_at=None, status="empty")
    stats(db, PACHECO, rush_yds=70)
    code = calibration_cli.main(
        ["--seasons", "2025", "--recompute", "--out", str(tmp_path)], session=db, params=SHAPES
    )
    assert code == 1
    assert calibration_cli.closing_snapshots(db, [2025], "close") == {}


def test_other_seasons_and_other_snapshots_are_not_mixed_in(db: Session) -> None:
    closing(db)
    closing(db, game=GAME_B)
    assert set(calibration_cli.closing_snapshots(db, [2025], "close")) == {GAME_A, GAME_B}
    assert calibration_cli.closing_snapshots(db, [2024], "close") == {}
    assert calibration_cli.closing_snapshots(db, [2025], "t24h") == {}


def test_bad_arguments_are_rejected(tmp_path: Path) -> None:
    for argv in (["--seasons", "abc"], ["--seasons", ""], ["--seasons", "2025", "--buckets", "1"]):
        with pytest.raises(SystemExit):
            calibration_cli.main(argv)
