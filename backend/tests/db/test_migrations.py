import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text

HYPERTABLES = {"prop_snapshots", "consensus_snapshots", "projections"}


def _hypertables(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT hypertable_name FROM timescaledb_information.hypertables"))
        return {row[0] for row in rows}


def _continuous_aggregates(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT view_name FROM timescaledb_information.continuous_aggregates")
        )
        return {row[0] for row in rows}


@pytest.mark.usefixtures("engine")
def test_migrations_run_down_and_up_cleanly(db_url: str, alembic_config: Config) -> None:
    probe = create_engine(db_url)
    try:
        command.downgrade(alembic_config, "base")
        assert set(inspect(probe).get_table_names()) == {"alembic_version"}
        assert _hypertables(probe) == set()
        assert _continuous_aggregates(probe) == set()

        command.upgrade(alembic_config, "head")
        assert {"players", "games", "prop_snapshots", "rosters"} <= set(
            inspect(probe).get_table_names()
        )
        assert _hypertables(probe) == HYPERTABLES
        assert _continuous_aggregates(probe) == {"consensus_hourly"}
    finally:
        probe.dispose()


@pytest.mark.usefixtures("engine")
def test_models_match_migrations(alembic_config: Config) -> None:
    command.check(alembic_config)
