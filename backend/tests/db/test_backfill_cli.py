from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.settings import Settings
from db.models import BackfillProgress, Game, IngestionRun
from db.seed import seed
from ingest import backfill_cli
from ingest.nflverse.source import ParquetSource
from ingest.odds.provider import OddsEvent, OddsProviderError
from tests.db.backfill_support import (
    EVENT_A,
    EVENT_B,
    GAME_A,
    KICKOFF_A,
    FakeHistory,
    build_world,
    events_for_games,
)

POSTED = KICKOFF_A - timedelta(hours=120)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nflverse"


@pytest.fixture
def db(session: Session) -> Iterator[Session]:
    yield build_world(session)


def run(db: Session, *argv: str, provider: FakeHistory | None = None) -> int:
    return backfill_cli.main(
        ["--seasons", "2025", "--skip-nflverse", *argv],
        session=db,
        provider=provider,
        sleep=lambda _: None,
    )


def rows(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


# Arguments -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--seasons", "abc"],
        ["--seasons", ""],
        ["--seasons", "2025", "--weeks", "x"],
        ["--seasons", "2025", "--snapshots", "open,noon"],
        ["--seasons", "2025", "--snapshots", ""],
        ["--seasons", "2025", "--max-rps", "0"],
        ["--seasons", "2025", "--reserve", "-1"],
    ],
)
def test_bad_arguments_are_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        backfill_cli.parse_args(argv)


def test_arguments_are_parsed() -> None:
    args = backfill_cli.parse_args(
        ["--seasons", "2025,2023", "--weeks", "1-3,8", "--snapshots", "close,open,t24h"]
    )
    assert args.seasons == [2023, 2025]
    assert args.weeks == [1, 2, 3, 8]
    assert args.snapshots == ["open", "t24h", "close"]  # in schedule order
    assert (args.confirm, args.reserve, args.max_rps) == (False, 1000, 2.0)


def test_market_selection() -> None:
    parse = backfill_cli.parse_args
    default = backfill_cli.choose_markets(parse(["--seasons", "2025"]))
    assert len(default) == 9
    assert (
        len(backfill_cli.choose_markets(parse(["--seasons", "2025", "--include-alternates"]))) == 14
    )
    assert backfill_cli.choose_markets(parse(["--seasons", "2025", "--markets", "a, b"])) == [
        "a",
        "b",
    ]


# The estimate and the confirm gate -------------------------------------------------------------


def test_without_confirm_it_prints_the_estimate_and_spends_nothing(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeHistory(posted={EVENT_A: POSTED})
    assert run(db, provider=provider) == 0
    out = capsys.readouterr().out

    assert "Backfill estimate: 2 games x 6 snapshots" in out
    assert "Nothing was spent. Add --confirm" in out
    assert provider.calls == [] and provider.event_calls == []
    assert rows(db, IngestionRun) == 0


def test_the_estimate_needs_no_api_key(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(db) == 0  # no provider injected; an offline stand-in would raise if used
    assert "Backfill estimate" in capsys.readouterr().out


def test_no_games_is_an_error(session: Session, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(session) == 1
    assert "No games found" in capsys.readouterr().out


def test_confirming_without_a_key_exits_with_a_message(
    db: Session, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_key(
        settings: Settings, *, used_today: int = 0, credit_budget: int | None = None
    ) -> None:
        raise ValueError("ODDS_API_KEY is not set")

    monkeypatch.setattr(backfill_cli, "get_odds_provider", no_key)
    assert run(db, "--confirm") == 2
    assert "ODDS_API_KEY is not set" in capsys.readouterr().err
    assert rows(db, BackfillProgress) == 0


# Spending --------------------------------------------------------------------------------------


def test_confirm_runs_the_backfill_and_prints_the_report(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeHistory(posted={EVENT_A: POSTED, EVENT_B: POSTED})
    assert run(db, "--confirm", provider=provider) == 0
    out = capsys.readouterr().out

    assert "Spending up to" in out
    assert "Run 1: ok. 2 games finished" in out
    assert "2025: 2 games (2 linked to events). Snapshots: 12 done, 0 empty, 0 stale." in out
    assert "under the 2% gate" in out
    assert rows(db, BackfillProgress) == 12

    # A second run has nothing left to buy.
    assert run(db, "--confirm", provider=provider) == 0
    assert "Nothing left to capture" in capsys.readouterr().out
    assert provider.paid_calls == 12


def test_max_credits_caps_the_spend_and_says_how_to_continue(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeHistory(posted={EVENT_A: POSTED, EVENT_B: POSTED}, budget=3 * 20)
    two_markets = ("--markets", "player_rush_yds,player_anytime_td")
    assert run(db, "--confirm", "--max-credits", "60", *two_markets, provider=provider) == 0
    out = capsys.readouterr().out
    assert "Spending up to 60 credits" in out
    assert "Stopped early (credit cap). Re-run the same command to continue." in out
    assert provider.used == 60


def test_an_error_on_one_game_exits_nonzero(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeHistory(
        posted={EVENT_A: POSTED, EVENT_B: POSTED}, errors={EVENT_A: "HTTP 503: down"}
    )
    assert run(db, "--confirm", provider=provider) == 1
    assert f"error: {GAME_A}: OddsProviderError: HTTP 503: down" in capsys.readouterr().out


def test_unlinked_games_are_linked_before_capturing(
    session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_world(session, linked=False)
    provider = FakeHistory(posted={EVENT_A: POSTED, EVENT_B: POSTED}, events=events_for_games())
    assert run(db, "--confirm", provider=provider) == 0
    out = capsys.readouterr().out

    assert "Linked 2 games to historical events in 1 weekly calls (1 credits)" in out
    assert db.get(Game, GAME_A).odds_event_id == EVENT_A  # type: ignore[union-attr]
    assert rows(db, BackfillProgress) == 12


def test_a_snapshot_subset_and_week_filter_are_honoured(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeHistory(posted={EVENT_A: POSTED, EVENT_B: POSTED})
    assert run(db, "--confirm", "--snapshots", "t24h,close", "--weeks", "8", provider=provider) == 0
    labels = set(db.scalars(select(BackfillProgress.label)))
    assert labels == {"t24h", "close"}
    assert rows(db, BackfillProgress) == 4

    assert run(db, "--confirm", "--weeks", "9", provider=provider) == 1  # no week-9 games
    assert "No games found for those seasons and weeks." in capsys.readouterr().out
    assert rows(db, BackfillProgress) == 4


# Probe, report and prerequisites ---------------------------------------------------------------


def test_the_probe_makes_one_historical_events_call(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeHistory(posted={}, events=events_for_games())
    assert run(db, "--probe", "--confirm", provider=provider) == 0
    assert "Historical data works: 1 event listed" in capsys.readouterr().out
    assert len(provider.event_calls) == 1
    assert provider.calls == []


def test_the_probe_reports_a_key_without_history(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    class NoHistory(FakeHistory):
        def list_historical_events(self, *args: object, **kwargs: object) -> list[OddsEvent]:
            raise OddsProviderError("HTTP 401: only available on paid usage plans")

    assert run(db, "--probe", "--confirm", provider=NoHistory(posted={})) == 1
    assert "Historical data is not available on this key" in capsys.readouterr().out


def test_the_probe_still_needs_confirm(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    provider = FakeHistory(posted={}, events=events_for_games())
    assert run(db, "--probe", provider=provider) == 0
    assert "Nothing was spent" in capsys.readouterr().out
    assert provider.event_calls == []


def test_report_shows_status_without_touching_the_api(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    run(db, "--confirm", provider=FakeHistory(posted={EVENT_A: POSTED, EVENT_B: POSTED}))
    capsys.readouterr()
    assert run(db, "--report") == 0
    out = capsys.readouterr().out
    assert "2025: 2 games" in out
    assert "Backfill estimate" not in out


def test_the_free_nflverse_data_is_loaded_first(
    session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(session)
    session.commit()
    code = backfill_cli.main(
        ["--seasons", "2026"],
        session=session,
        nflverse=ParquetSource(FIXTURES),
        sleep=lambda _: None,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "nflverse 2026: schedules 272" in out
    assert "player_stats" in out
    assert "Backfill estimate: 272 games x 6 snapshots" in out


# Locking ---------------------------------------------------------------------------------------


def test_only_one_backfill_runs_at_a_time(engine: Engine) -> None:
    with backfill_cli.backfill_lock(engine) as first:
        assert first is True
        with backfill_cli.backfill_lock(engine) as second:
            assert second is False
    with backfill_cli.backfill_lock(engine) as again:
        assert again is True
