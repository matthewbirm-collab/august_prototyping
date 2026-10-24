from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.settings import Settings
from db.models import IngestionRun
from ingest import run_ingest
from ingest.odds.factory import get_odds_provider
from ingest.odds.markets import ALTERNATE_MARKETS, MAIN_MARKETS
from ingest.odds.provider import OddsProviderError
from tests.db.support import DAY, FAR, NOW, SOON, FakeOdds, standard_responses


@pytest.fixture
def db(capture_db: Session) -> Session:
    return capture_db


def clock() -> Any:
    return NOW


def runs(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(IngestionRun)) or 0


# Arguments -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--tier", "7x"],
        ["--tier", ""],
        ["--serve", "--dry-run"],
        ["--serve-interval-minutes", "0"],
    ],
)
def test_bad_arguments_are_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        run_ingest.parse_args(argv)


def test_market_selection() -> None:
    parse = run_ingest.parse_args
    assert run_ingest.choose_markets(parse([])) == list(MAIN_MARKETS)
    assert run_ingest.choose_markets(parse(["--include-alternates"])) == [
        *MAIN_MARKETS,
        *ALTERNATE_MARKETS,
    ]
    assert run_ingest.choose_markets(parse(["--markets", "player_pass_yds, player_rush_yds"])) == [
        "player_pass_yds",
        "player_rush_yds",
    ]
    assert parse(["--bookmakers", "draftkings,fanduel"]).bookmakers == ["draftkings", "fanduel"]


# Dry run and runs ------------------------------------------------------------------------------


def test_dry_run_projects_spend_and_lists_due_games_without_calling_the_api(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_ingest.main(["--dry-run"], session=db, clock=clock) == 0
    out = capsys.readouterr().out

    assert "Projected spend for the next 7 days: 4 games" in out
    assert "Due now (auto): 3 games" in out
    for game_id in (SOON, DAY, FAR):
        assert game_id in out
    assert "1 upcoming games have no odds event yet" in out
    assert runs(db) == 0


def test_dry_run_can_show_a_single_tier(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    run_ingest.main(["--dry-run", "--tier", "15m"], session=db, clock=clock)
    out = capsys.readouterr().out
    assert "Due now (15m): 1 games" in out
    assert SOON in out
    assert DAY not in out.split("Due now")[1]


def test_a_run_prints_its_summary_and_the_projection_the_first_time(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    odds = FakeOdds(standard_responses())
    assert run_ingest.main([], session=db, provider=odds, clock=clock) == 0
    out = capsys.readouterr().out
    assert "Projected spend" in out
    assert "Run 1: ok. 3 games, 6 quotes, 4 new snapshots, 0 unmatched names, 27 credits." in out

    assert run_ingest.main([], session=db, provider=odds, clock=clock) == 0
    again = capsys.readouterr().out
    assert again.strip().startswith("Nothing due.")
    assert "Projected spend" not in again


def test_a_partial_run_exits_nonzero(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    responses = standard_responses()
    responses["ev-day"] = OddsProviderError("HTTP 503: down")
    assert run_ingest.main([], session=db, provider=FakeOdds(responses), clock=clock) == 1
    out = capsys.readouterr().out
    assert "partial" in out
    assert f"error: {DAY}" in out


def test_a_spent_budget_exits_cleanly(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    odds = FakeOdds(standard_responses(), budget=15, used_today=15)
    assert run_ingest.main([], session=db, provider=odds, clock=clock) == 0
    assert "Daily credit budget spent; 3 due games deferred." in capsys.readouterr().out
    assert runs(db) == 0


def test_a_missing_api_key_exits_with_a_message(
    db: Session, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_key(settings: Settings, *, used_today: int = 0) -> None:
        raise ValueError("ODDS_API_KEY is not set")

    monkeypatch.setattr(run_ingest, "get_odds_provider", no_key)
    assert run_ingest.main([], session=db, clock=clock) == 2
    assert "ODDS_API_KEY is not set" in capsys.readouterr().err
    assert runs(db) == 0


@pytest.mark.parametrize("key", [None, ""])
def test_the_provider_factory_rejects_a_missing_key(key: str | None) -> None:
    settings = Settings(_env_file=None, odds_api_key=key)
    with pytest.raises(ValueError, match="ODDS_API_KEY"):
        get_odds_provider(settings)


# Serving and locking ---------------------------------------------------------------------------


def test_the_scheduler_runs_one_capture_at_a_time_on_an_interval() -> None:
    scheduler = run_ingest.build_scheduler(lambda: None, 5)
    [job] = scheduler.get_jobs()

    assert job.id == "capture"
    assert job.trigger.interval == timedelta(minutes=5)
    assert job.max_instances == 1
    assert job.coalesce is True
    assert not scheduler.running


def test_the_run_lock_admits_one_run_at_a_time(engine: Engine) -> None:
    with run_ingest.run_lock(engine) as first:
        assert first is True
        with run_ingest.run_lock(engine) as second:
            assert second is False
    with run_ingest.run_lock(engine) as again:
        assert again is True
