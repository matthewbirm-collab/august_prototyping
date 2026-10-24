from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.base import Base
from db.models import (
    ActualStats,
    DepthChart,
    FfRanking,
    Game,
    IngestionRun,
    Injury,
    Player,
    Roster,
    SnapCount,
)
from db.seed import seed
from ingest.nflverse import cli
from ingest.nflverse.load import DATASETS, NflverseLoader
from ingest.nflverse.odds_match import match_odds_events
from ingest.nflverse.source import ParquetSource
from ingest.odds.provider import OddsEvent

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nflverse"
HARRISON = "00-0039849"


@pytest.fixture
def db(session: Session) -> Iterator[Session]:
    seed(session)
    session.commit()
    yield session


@pytest.fixture
def loader(db: Session) -> NflverseLoader:
    return NflverseLoader(db, ParquetSource(FIXTURES))


def count(db: Session, model: type[Base]) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def test_full_season_load(db: Session, loader: NflverseLoader) -> None:
    counts = loader.run(2026)

    assert set(counts) == set(DATASETS)
    assert all(counts[d] > 0 for d in DATASETS), counts
    for model in (Game, Player, Roster, Injury, DepthChart, SnapCount, ActualStats, FfRanking):
        assert count(db, model) > 0, model.__tablename__

    run = db.scalars(select(IngestionRun)).one()
    assert (run.kind, run.status, run.errors) == ("nflverse", "ok", [])
    assert run.details["rows"] == counts
    assert run.finished_at is not None


def test_reload_is_idempotent(db: Session, loader: NflverseLoader) -> None:
    loader.run(2026)
    models = (Game, Player, Roster, Injury, DepthChart, SnapCount, ActualStats, FfRanking)
    before = {m.__tablename__: count(db, m) for m in models}
    loader.run(2026)
    assert {m.__tablename__: count(db, m) for m in models} == before


def test_traded_player_rosters_and_profile(db: Session, loader: NflverseLoader) -> None:
    loader.run(2026, datasets=["schedules", "players", "rosters"])
    harrison = db.scalars(select(Player).where(Player.gsis_id == HARRISON)).one()
    assert (harrison.full_name, harrison.team_abbr) == ("Marvin Harrison Jr.", "HOU")
    assert "sleeper" in harrison.external_ids

    weeks = dict(
        db.execute(
            select(Roster.week, Roster.team_abbr).where(Roster.player_id == harrison.id)
        ).all()
    )
    assert weeks == {1: "ARI", 2: "ARI", 3: "ARI", 4: "ARI", 5: "ARI", 6: "HOU"}


def test_week_filter_loads_only_that_week(db: Session, loader: NflverseLoader) -> None:
    loader.run(2026, datasets=["schedules", "players"])
    loader.run(2026, week=6, datasets=["rosters", "player_stats", "depth_charts"])
    assert set(db.scalars(select(Roster.week).distinct())) == {6}
    assert set(db.scalars(select(ActualStats.week).distinct())) == {6}
    assert set(db.scalars(select(DepthChart.week).distinct())) == {6}


def test_older_season_formats(db: Session, loader: NflverseLoader) -> None:
    loader.run(2026, datasets=["schedules", "players"])
    loader.run(2024, datasets=["schedules", "injuries", "depth_charts"])
    injuries = db.scalars(select(Injury).where(Injury.season == 2024)).all()
    assert injuries
    assert all(i.available_at.year == 2024 for i in injuries)
    depth = db.scalars(select(DepthChart).where(DepthChart.season == 2024)).all()
    assert depth
    assert {d.depth_rank for d in depth} <= {1, 2, 3}


def test_failed_dataset_is_recorded_and_others_still_load(
    db: Session, loader: NflverseLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(season: int, week: int | None) -> int:
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(loader, "load_injuries", boom)
    counts = loader.run(2026, datasets=["schedules", "players", "injuries"])
    assert set(counts) == {"schedules", "players"}
    run = db.scalars(select(IngestionRun)).one()
    assert run.status == "partial"
    assert run.errors == [{"dataset": "injuries", "error": "RuntimeError: source unavailable"}]


def test_unknown_dataset_is_rejected(loader: NflverseLoader) -> None:
    with pytest.raises(ValueError, match="unknown datasets"):
        loader.run(2026, datasets=["schedules", "tweets"])


# Odds event matching ---------------------------------------------------------------------------


def _event(event_id: str, home: str, away: str, kickoff: datetime) -> OddsEvent:
    return OddsEvent(event_id=event_id, home_team=home, away_team=away, commence_time=kickoff)


def test_match_odds_events(db: Session, loader: NflverseLoader) -> None:
    loader.run(2026, datasets=["schedules"])
    hou_jax = db.get(Game, "2026_06_HOU_JAX")
    ari_la = db.get(Game, "2026_06_ARI_LA")
    assert hou_jax is not None and ari_la is not None

    report = match_odds_events(
        db,
        [
            _event("ev-london", "Jacksonville Jaguars", "Houston Texans", hou_jax.kickoff_utc),
            # Swapped home/away and a few hours off still match.
            _event(
                "ev-swapped",
                "Arizona Cardinals",
                "Los Angeles Rams",
                ari_la.kickoff_utc + timedelta(hours=3),
            ),
            _event("ev-ghost", "Arizona Cardinals", "Houston Texans", hou_jax.kickoff_utc),
            _event("ev-unknown-team", "London Monarchs", "Houston Texans", hou_jax.kickoff_utc),
        ],
    )
    db.commit()

    assert report.matched == {"2026_06_HOU_JAX": "ev-london", "2026_06_ARI_LA": "ev-swapped"}
    assert [e.event_id for e in report.unmatched_events] == ["ev-ghost", "ev-unknown-team"]
    assert db.get(Game, "2026_06_HOU_JAX").odds_event_id == "ev-london"  # type: ignore[union-attr]


def test_schedule_reload_keeps_odds_event_ids(db: Session, loader: NflverseLoader) -> None:
    loader.run(2026, datasets=["schedules"])
    game = db.get(Game, "2026_06_HOU_JAX")
    assert game is not None
    game.odds_event_id = "ev-london"
    db.commit()
    loader.run(2026, datasets=["schedules"])
    db.expire_all()
    assert db.get(Game, "2026_06_HOU_JAX").odds_event_id == "ev-london"  # type: ignore[union-attr]


# CLI -------------------------------------------------------------------------------------------


class FakeOdds:
    def __init__(self, events: list[OddsEvent]) -> None:
        self.events = events
        self.windows: list[tuple[datetime, datetime]] = []

    def list_events(self, commence_from: datetime, commence_to: datetime) -> list[OddsEvent]:
        self.windows.append((commence_from, commence_to))
        return [e for e in self.events if commence_from <= e.commence_time <= commence_to]


def test_cli_loads_selected_datasets(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(
        ["--season", "2026", "--datasets", "schedules,players"],
        session=db,
        source=ParquetSource(FIXTURES),
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "schedules: 272" in out
    assert count(db, Roster) == 0


def test_cli_match_odds(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    source = ParquetSource(FIXTURES)
    cli.main(["--season", "2026", "--datasets", "schedules"], session=db, source=source)
    kickoff = db.get(Game, "2026_06_HOU_JAX").kickoff_utc  # type: ignore[union-attr]
    odds = FakeOdds([_event("ev-london", "Jacksonville Jaguars", "Houston Texans", kickoff)])

    code = cli.main(
        ["--season", "2026", "--week", "6", "--datasets", "schedules", "--match-odds"],
        session=db,
        source=source,
        odds=odds,  # type: ignore[arg-type]
    )
    assert code == 0
    assert "1 matched, 0 unmatched" in capsys.readouterr().out
    first, last = odds.windows[0]
    assert first < kickoff < last


def test_cli_rejects_bad_arguments() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["--season", "2026", "--datasets", "tweets"])
    with pytest.raises(SystemExit):
        cli.parse_args(["--season", "2026", "--match-odds"])


def test_kickoffs_stored_in_utc(db: Session, loader: NflverseLoader) -> None:
    loader.run(2026, datasets=["schedules"])
    game = db.get(Game, "2026_06_HOU_JAX")
    assert game is not None
    assert game.kickoff_utc == datetime(2026, 10, 18, 13, 30, tzinfo=UTC)
