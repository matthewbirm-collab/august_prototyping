"""Fixtures for tests that need Postgres + TimescaleDB (TEST_DATABASE_URL).

Locally these skip when the test database is unreachable (run `make up` or `make setup`).
In CI (`CI` set) they fail instead, so a missing database can't pass silently.
"""

import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.settings import get_settings
from db.base import Base
from db.models import Game, Player, Roster
from db.seed import seed
from tests.db.support import GAMES, NOW, PLAYERS

BACKEND_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def db_url() -> str:
    url = get_settings().test_database_url
    try:
        with create_engine(url).connect():
            pass
    except OperationalError as exc:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"test database unavailable ({url}): {exc.orig}")
    return url


@pytest.fixture(scope="session")
def alembic_config(db_url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", db_url)
    return config


@pytest.fixture(scope="session")
def engine(db_url: str, alembic_config: Config) -> Iterator[Engine]:
    """A test database migrated from scratch to head."""
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    db_engine = create_engine(db_url)
    yield db_engine
    db_engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session on an empty, migrated database; every table is truncated afterwards."""
    with Session(engine) as db_session:
        yield db_session
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def capture_db(session: Session) -> Iterator[Session]:
    """Seeded reference data, six games around NOW (see tests/db/support.py) and five players."""
    seed(session)
    for game_id, week, home, away, offset, event in GAMES:
        session.add(
            Game(
                id=game_id,
                season=2026,
                week=week,
                season_type="REG",
                kickoff_utc=NOW + offset,
                home_abbr=home,
                away_abbr=away,
                odds_event_id=event,
            )
        )
    for player_id, name, team, position in PLAYERS:
        session.add(Player(id=player_id, full_name=name, position=position))
        session.flush()
        session.add(
            Roster(
                season=2026,
                week=8,
                player_id=player_id,
                team_abbr=team,
                position=position,
                available_at=NOW - timedelta(days=5),
            )
        )
    session.commit()
    yield session
