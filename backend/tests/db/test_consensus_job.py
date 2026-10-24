from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.settings import Settings
from db.models import Book, ConsensusSnapshot
from db.snapshots import SnapshotQuote, store_prop_snapshots
from engine.consensus import STUB_LINE_CV, BookQuote, ConsensusConfig, consensus
from ingest import run_ingest
from ingest.consensus_job import ConsensusJob, JobConfig
from ingest.recompute import RecomputeRequest
from tests.db import backfill_support as world
from tests.db import support
from tests.db.backfill_support import GAME_A, GAME_B

AS_OF = datetime(2025, 10, 19, 12, 0, tzinfo=UTC)  # five hours before GAME_A kicks off
PACHECO, KELCE, LOVE, SWIFT = 1, 2, 3, 4
RUSH, RUSH_ALT, TD = "player_rush_yds", "player_rush_yds_alternate", "player_anytime_td"


@pytest.fixture
def db(session: Session) -> Iterator[Session]:
    yield world.build_world(session)


def put(
    db: Session,
    book: str,
    market: str,
    player: int,
    line: float | None,
    over: int,
    under: int | None,
    minutes_before: float = 2,
    game: str = GAME_A,
) -> None:
    quote = SnapshotQuote(
        captured_at=AS_OF - timedelta(minutes=minutes_before),
        game_id=game,
        book_key=book,
        market_key=market,
        player_id=player,
        line=line,
        over_price=over,
        under_price=under,
        source="live",
    )
    store_prop_snapshots(db, [quote])
    db.commit()


def rows(db: Session, game: str = GAME_A) -> dict[tuple[str, int], list[ConsensusSnapshot]]:
    found: dict[tuple[str, int], list[ConsensusSnapshot]] = {}
    stmt = (
        select(ConsensusSnapshot)
        .where(ConsensusSnapshot.game_id == game)
        .order_by(ConsensusSnapshot.captured_at)
    )
    for row in db.scalars(stmt):
        found.setdefault((row.market_key, row.player_id), []).append(row)
    return found


def book_quote(
    book: str, line: float | None, over: int, under: int | None, minutes: float = 2
) -> BookQuote:
    return BookQuote(book, line, over, under, AS_OF - timedelta(minutes=minutes))


def rush_quotes(db: Session) -> None:
    put(db, "draftkings", RUSH, PACHECO, 64.5, -110, -110)
    put(db, "fanduel", RUSH, PACHECO, 65.5, -110, -110)
    put(db, "betmgm", RUSH, PACHECO, 64.5, -120, 100)


# What gets written -----------------------------------------------------------------------------


def test_one_row_per_player_and_main_market_matching_the_engine(db: Session) -> None:
    rush_quotes(db)
    put(db, "draftkings", TD, KELCE, None, 120, None)
    put(db, "fanduel", TD, KELCE, None, 110, None)

    result = ConsensusJob(db).recompute(GAME_A, None, AS_OF)

    assert (result.written, result.unchanged, result.no_consensus) == (2, 0, 0)
    found = rows(db)
    assert set(found) == {(RUSH, PACHECO), (TD, KELCE)}

    expected = consensus(
        [
            book_quote("draftkings", 64.5, -110, -110),
            book_quote("fanduel", 65.5, -110, -110),
            book_quote("betmgm", 64.5, -120, 100),
        ],
        config=ConsensusConfig(line_cv=STUB_LINE_CV["rush_yds"]),
        as_of=AS_OF,
    )
    [rush] = found[(RUSH, PACHECO)]
    assert expected is not None
    assert (rush.fair_line, rush.books_count) == (64.5, 3)
    assert rush.fair_p_over == pytest.approx(expected.fair_p_over)
    assert rush.dispersion == pytest.approx(expected.dispersion)
    assert (rush.captured_at, rush.game_id, rush.player_id) == (AS_OF, GAME_A, PACHECO)

    [td] = found[(TD, KELCE)]
    assert (td.fair_line, td.books_count) == (None, 2)
    assert td.dispersion is not None and td.dispersion < 0.05
    assert 0.35 < td.fair_p_over < 0.50  # about 1 / (2.15 x 1.1) with the assumed 10% margin


def test_alternate_markets_are_left_to_the_distribution_step(db: Session) -> None:
    put(db, "draftkings", RUSH_ALT, PACHECO, 74.5, 150, None)
    put(db, "fanduel", RUSH_ALT, PACHECO, 74.5, 160, None)
    result = ConsensusJob(db).recompute(GAME_A, None, AS_OF)
    assert result.written == 0
    assert rows(db) == {}


def test_only_the_requested_players_are_recomputed(db: Session) -> None:
    rush_quotes(db)
    put(db, "draftkings", TD, KELCE, None, 120, None)

    ConsensusJob(db).recompute(GAME_A, [PACHECO], AS_OF)
    assert set(rows(db)) == {(RUSH, PACHECO)}
    ConsensusJob(db).recompute(GAME_A, [KELCE], AS_OF)
    assert set(rows(db)) == {(RUSH, PACHECO), (TD, KELCE)}


def test_games_are_kept_apart(db: Session) -> None:
    rush_quotes(db)
    put(db, "draftkings", RUSH, SWIFT, 55.5, -110, -110, game=GAME_B)
    ConsensusJob(db).recompute(GAME_A, None, AS_OF)
    assert set(rows(db, GAME_A)) == {(RUSH, PACHECO)}
    assert rows(db, GAME_B) == {}


def test_the_recompute_request_is_what_the_capture_hook_passes(db: Session) -> None:
    rush_quotes(db)
    ConsensusJob(db)(RecomputeRequest(GAME_A, (PACHECO,), AS_OF))
    assert set(rows(db)) == {(RUSH, PACHECO)}


# Unchanged, changed and aging ------------------------------------------------------------------


def test_an_identical_consensus_is_not_written_again(db: Session) -> None:
    rush_quotes(db)
    job = ConsensusJob(db)
    first = job.recompute(GAME_A, None, AS_OF)
    again = job.recompute(GAME_A, None, AS_OF + timedelta(minutes=30))

    assert (first.written, again.written, again.unchanged) == (1, 0, 1)
    assert len(rows(db)[(RUSH, PACHECO)]) == 1


def test_a_changed_price_writes_a_new_row_and_keeps_the_old_one(db: Session) -> None:
    rush_quotes(db)
    job = ConsensusJob(db)
    job.recompute(GAME_A, None, AS_OF)

    later = AS_OF + timedelta(minutes=20)
    store_prop_snapshots(
        db,
        [SnapshotQuote(later, GAME_A, "draftkings", RUSH, PACHECO, 64.5, -150, 130, "live")],
    )
    db.commit()
    result = job.recompute(GAME_A, None, later)

    assert result.written == 1
    history = rows(db)[(RUSH, PACHECO)]
    assert [r.captured_at for r in history] == [AS_OF, later]
    assert history[1].fair_p_over > history[0].fair_p_over


def test_a_recompute_in_the_past_ignores_quotes_from_the_future(db: Session) -> None:
    rush_quotes(db)
    put(db, "betrivers", RUSH, PACHECO, 64.5, -150, 130, minutes_before=-10)  # after AS_OF
    ConsensusJob(db).recompute(GAME_A, None, AS_OF)
    [row] = rows(db)[(RUSH, PACHECO)]
    assert row.books_count == 3


def test_quotes_age_out_after_the_stale_limit(db: Session) -> None:
    rush_quotes(db)
    job = ConsensusJob(db)
    assert job.recompute(GAME_A, None, AS_OF + timedelta(minutes=60)).written == 1
    gone = job.recompute(GAME_A, None, AS_OF + timedelta(minutes=100))  # 98 minutes old
    # Nothing is left inside the window, so the market isn't visited and the old row stands.
    assert gone.written == 0
    assert len(rows(db)[(RUSH, PACHECO)]) == 1


def test_one_book_going_quiet_changes_the_consensus(db: Session) -> None:
    put(db, "draftkings", RUSH, PACHECO, 64.5, -110, -110, minutes_before=2)
    put(db, "fanduel", RUSH, PACHECO, 64.5, -150, 130, minutes_before=2)
    job = ConsensusJob(db)
    job.recompute(GAME_A, None, AS_OF)

    # Fifty minutes later DraftKings has posted again but FanDuel has not (and is still fresh
    # enough); at 100 minutes FanDuel is stale and drops out.
    put(db, "draftkings", RUSH, PACHECO, 64.5, -112, -108, minutes_before=-98)
    result = job.recompute(GAME_A, None, AS_OF + timedelta(minutes=100))
    assert result.written == 1
    assert rows(db)[(RUSH, PACHECO)][-1].books_count == 1


def test_the_latest_quote_from_each_book_is_the_one_used(db: Session) -> None:
    put(db, "draftkings", RUSH, PACHECO, 64.5, -110, -110, minutes_before=30)
    put(db, "draftkings", RUSH, PACHECO, 64.5, -150, 130, minutes_before=5)
    ConsensusJob(db).recompute(GAME_A, None, AS_OF)
    expected = consensus(
        [book_quote("draftkings", 64.5, -150, 130, 5)],
        config=ConsensusConfig(line_cv=STUB_LINE_CV["rush_yds"]),
        as_of=AS_OF,
    )
    [row] = rows(db)[(RUSH, PACHECO)]
    assert expected is not None
    assert (row.books_count, row.dispersion) == (1, None)
    assert row.fair_p_over == pytest.approx(expected.fair_p_over)


# Weights and settings --------------------------------------------------------------------------


def test_sharpness_weights_come_from_the_books_table(db: Session) -> None:
    rush_quotes(db)
    db.execute(update(Book).where(Book.key == "fanduel").values(sharpness_weight=4.0))
    db.commit()
    ConsensusJob(db).recompute(GAME_A, None, AS_OF)

    weights = {"draftkings": 1.0, "fanduel": 4.0, "betmgm": 1.0}
    expected = consensus(
        [
            book_quote("draftkings", 64.5, -110, -110),
            book_quote("fanduel", 65.5, -110, -110),
            book_quote("betmgm", 64.5, -120, 100),
        ],
        weights,
        ConsensusConfig(line_cv=STUB_LINE_CV["rush_yds"]),
        AS_OF,
    )
    [row] = rows(db)[(RUSH, PACHECO)]
    assert expected is not None
    assert row.fair_line == 65.5  # FanDuel now carries most of the weight
    assert row.fair_p_over == pytest.approx(expected.fair_p_over)


def test_settings_set_the_method_the_margin_and_the_windows() -> None:
    settings = Settings(
        _env_file=None,
        devig_method="power",
        one_sided_overround=0.15,
        consensus_half_life_minutes=30,
        stale_quote_minutes=60,
    )
    config = JobConfig.from_settings(settings).consensus
    assert config.method == "power"
    assert config.one_sided_overround == 0.15
    assert config.half_life == timedelta(minutes=30)
    assert config.stale_after == timedelta(minutes=60)


def test_the_assumed_margin_changes_one_sided_probabilities(db: Session) -> None:
    put(db, "draftkings", TD, KELCE, None, 120, None)
    ConsensusJob(db, JobConfig(ConsensusConfig(one_sided_overround=0.0))).recompute(
        GAME_A, None, AS_OF
    )
    ConsensusJob(db, JobConfig(ConsensusConfig(one_sided_overround=0.2))).recompute(
        GAME_A, None, AS_OF + timedelta(minutes=1)
    )
    none, high = rows(db)[(TD, KELCE)]
    assert none.fair_p_over == pytest.approx(100 / 220)
    assert high.fair_p_over == pytest.approx(100 / 220 / 1.2)


def test_dropping_one_sided_quotes_leaves_no_consensus_for_a_yes_only_market(db: Session) -> None:
    put(db, "draftkings", TD, KELCE, None, 120, None)
    config = JobConfig(replace(ConsensusConfig(), one_sided_overround=None))
    result = ConsensusJob(db, config).recompute(GAME_A, None, AS_OF)
    assert (result.written, result.no_consensus) == (0, 1)


# Wired into the capture ------------------------------------------------------------------------


@pytest.fixture
def capture_world(capture_db: Session) -> Session:
    return capture_db


def consensus_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(ConsensusSnapshot)) or 0


def test_the_capture_writes_consensus_for_the_players_with_new_quotes(
    capture_world: Session,
) -> None:
    db = capture_world
    capture = support.make_capture(
        db, support.FakeOdds(support.standard_responses()), hook=ConsensusJob(db)
    )
    capture.run("15m")  # only the game kicking off within three hours

    found = rows(db, support.SOON)
    assert set(found) == {("player_rush_yds", 1), ("player_anytime_td", 2)}
    rush = found[("player_rush_yds", 1)][0]
    assert (rush.books_count, rush.dispersion, rush.captured_at) == (1, None, support.NOW)
    assert rush.fair_p_over == pytest.approx(0.5, abs=0.05)

    before = consensus_count(db)
    capture.run("15m")  # nothing changed, so there is nothing to recompute
    assert consensus_count(db) == before


def test_run_ingest_updates_consensus_after_every_run(capture_world: Session) -> None:
    db = capture_world
    odds = support.FakeOdds(support.standard_responses())

    code = run_ingest.main([], session=db, provider=odds, clock=lambda: support.NOW)

    assert code == 0
    # Rushing and the touchdown line in one game, a passing line in each of the other two.
    assert consensus_count(db) == 4
    assert {r.game_id for r in db.scalars(select(ConsensusSnapshot))} == {
        support.SOON,
        support.DAY,
        support.FAR,
    }
