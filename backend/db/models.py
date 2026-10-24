"""SQLAlchemy models. See docs/DATA_MODEL.md for the design.

These mirror the hand-written Alembic migrations; `alembic check` (run in the test suite) fails
if they drift apart. Hypertables, the continuous aggregate and CHECK constraints live only in the
migrations, since autogenerate can't express them.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

TIMESTAMPTZ = DateTime(timezone=True)


class Team(Base):
    __tablename__ = "teams"

    abbr: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    conference: Mapped[str] = mapped_column(Text)
    division: Mapped[str] = mapped_column(Text)


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    full_name: Mapped[str] = mapped_column(Text)
    position: Mapped[str] = mapped_column(Text)
    team_abbr: Mapped[str | None] = mapped_column(ForeignKey("teams.abbr"), index=True)
    gsis_id: Mapped[str | None] = mapped_column(Text, unique=True)
    external_ids: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    birth_date: Mapped[date | None] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


class Roster(Base):
    __tablename__ = "rosters"
    __table_args__ = (Index("ix_rosters_season_week_team", "season", "week", "team_abbr"),)

    season: Mapped[int] = mapped_column(Integer, primary_key=True)
    week: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), primary_key=True)
    team_abbr: Mapped[str] = mapped_column(ForeignKey("teams.abbr"))
    position: Mapped[str] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    available_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ)


class PlayerAlias(Base):
    __tablename__ = "player_aliases"
    __table_args__ = (
        UniqueConstraint("source", "raw_name", "player_id"),
        Index("ix_player_aliases_source_raw_name", "source", "raw_name"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    raw_name: Mapped[str] = mapped_column(Text)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    method: Mapped[str] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


class Game(Base):
    __tablename__ = "games"
    __table_args__ = (Index("ix_games_season_week", "season", "week"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    season: Mapped[int] = mapped_column(Integer)
    week: Mapped[int] = mapped_column(Integer)
    season_type: Mapped[str] = mapped_column(Text)
    kickoff_utc: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    home_abbr: Mapped[str] = mapped_column(ForeignKey("teams.abbr"))
    away_abbr: Mapped[str] = mapped_column(ForeignKey("teams.abbr"))
    odds_event_id: Mapped[str | None] = mapped_column(Text, unique=True)
    spread_line: Mapped[float | None] = mapped_column(Float)
    total_line: Mapped[float | None] = mapped_column(Float)


class UnmatchedName(Base):
    __tablename__ = "unmatched_names"
    __table_args__ = (UniqueConstraint("source", "raw_name", "game_id"),)

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    raw_name: Mapped[str] = mapped_column(Text)
    team_hint: Mapped[str | None] = mapped_column(Text)
    position_hint: Mapped[str | None] = mapped_column(Text)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"))
    occurrences: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    first_seen: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    resolved_player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))


class Book(Base):
    __tablename__ = "books"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text)
    sharpness_weight: Mapped[float] = mapped_column(Float, server_default=text("1.0"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Market(Base):
    __tablename__ = "markets"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    stat: Mapped[str] = mapped_column(Text)
    distribution_family: Mapped[str] = mapped_column(Text)
    is_alternate: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    base_market_key: Mapped[str | None] = mapped_column(ForeignKey("markets.key"))


class ScoringConfig(Base):
    __tablename__ = "scoring_configs"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    key: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    weights: Mapped[dict[str, Any]] = mapped_column(JSONB)
    is_builtin: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    status: Mapped[str] = mapped_column(Text, server_default=text("'running'"))
    events: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    quotes: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    new_snapshots: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    credits_used: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    credits_remaining: Mapped[int | None] = mapped_column(Integer)
    errors: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class PropSnapshot(Base):
    """One book's quote for one player prop at one moment. TimescaleDB hypertable."""

    __tablename__ = "prop_snapshots"
    __table_args__ = (
        Index(
            "ix_prop_snapshots_player_market_book_time",
            "player_id",
            "market_key",
            "book_key",
            text("captured_at DESC"),
        ),
        Index("ix_prop_snapshots_game_time", "game_id", text("captured_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, primary_key=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"))
    book_key: Mapped[str] = mapped_column(ForeignKey("books.key"))
    market_key: Mapped[str] = mapped_column(ForeignKey("markets.key"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    line: Mapped[float | None] = mapped_column(Float)
    over_price: Mapped[int] = mapped_column(Integer)
    under_price: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[bytes] = mapped_column(LargeBinary)
    ingestion_run_id: Mapped[int | None] = mapped_column(ForeignKey("ingestion_runs.id"))


class ConsensusSnapshot(Base):
    """Multi-book fair line and P(over) for one player market. TimescaleDB hypertable."""

    __tablename__ = "consensus_snapshots"
    __table_args__ = (
        Index(
            "ix_consensus_snapshots_player_market_time",
            "player_id",
            "market_key",
            text("captured_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, primary_key=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    market_key: Mapped[str] = mapped_column(ForeignKey("markets.key"))
    fair_line: Mapped[float | None] = mapped_column(Float)
    fair_p_over: Mapped[float] = mapped_column(Float)
    books_count: Mapped[int] = mapped_column(Integer)
    dispersion: Mapped[float | None] = mapped_column(Float)


class Projection(Base):
    """A versioned fantasy projection computed from inputs available at `as_of`. Hypertable."""

    __tablename__ = "projections"
    __table_args__ = (
        Index(
            "ix_projections_player_config_time",
            "player_id",
            "scoring_config_id",
            text("computed_at DESC"),
        ),
        Index(
            "ix_projections_game_config_time",
            "game_id",
            "scoring_config_id",
            text("computed_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, primary_key=True)
    as_of: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    scoring_config_id: Mapped[int] = mapped_column(ForeignKey("scoring_configs.id"))
    mean: Mapped[float] = mapped_column(Float)
    p20: Mapped[float] = mapped_column(Float)
    p80: Mapped[float] = mapped_column(Float)
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSONB)
    confidence: Mapped[float] = mapped_column(Float)
    engine_version: Mapped[str] = mapped_column(Text)


class Injury(Base):
    __tablename__ = "injuries"

    season: Mapped[int] = mapped_column(Integer, primary_key=True)
    week: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), primary_key=True)
    available_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, primary_key=True)
    team_abbr: Mapped[str] = mapped_column(ForeignKey("teams.abbr"))
    report_status: Mapped[str | None] = mapped_column(Text)
    practice_status: Mapped[str | None] = mapped_column(Text)
    primary_injury: Mapped[str | None] = mapped_column(Text)


class DepthChart(Base):
    __tablename__ = "depth_charts"

    season: Mapped[int] = mapped_column(Integer, primary_key=True)
    week: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), primary_key=True)
    position: Mapped[str] = mapped_column(Text, primary_key=True)
    available_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, primary_key=True)
    team_abbr: Mapped[str] = mapped_column(ForeignKey("teams.abbr"))
    depth_rank: Mapped[int] = mapped_column(Integer)


class ActualStats(Base):
    """Box-score ground truth for grading projections."""

    __tablename__ = "actual_stats"

    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"), primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), primary_key=True)
    season: Mapped[int] = mapped_column(Integer)
    week: Mapped[int] = mapped_column(Integer)
    completions: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    pass_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    pass_yds: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    pass_tds: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    interceptions: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    rush_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    rush_yds: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    rush_tds: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    targets: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    receptions: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    rec_yds: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    rec_tds: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    fumbles_lost: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    two_pt_conversions: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class SnapCount(Base):
    """Per-game snap counts from Pro Football Reference via nflverse."""

    __tablename__ = "snap_counts"

    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"), primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), primary_key=True)
    season: Mapped[int] = mapped_column(Integer)
    week: Mapped[int] = mapped_column(Integer)
    team_abbr: Mapped[str] = mapped_column(ForeignKey("teams.abbr"))
    position: Mapped[str] = mapped_column(Text)
    offense_snaps: Mapped[int] = mapped_column(Integer)
    offense_pct: Mapped[float] = mapped_column(Float)
    defense_snaps: Mapped[int] = mapped_column(Integer)
    defense_pct: Mapped[float] = mapped_column(Float)
    st_snaps: Mapped[int] = mapped_column(Integer)
    st_pct: Mapped[float] = mapped_column(Float)


class FfRanking(Base):
    """FantasyPros weekly expert consensus rankings (ECR) via nflverse: the backtest baseline."""

    __tablename__ = "ff_rankings"
    __table_args__ = (
        UniqueConstraint("scrape_date", "page_type", "fantasypros_id"),
        Index("ix_ff_rankings_season_week", "season", "week"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    season: Mapped[int] = mapped_column(Integer)
    week: Mapped[int] = mapped_column(Integer)
    scrape_date: Mapped[date] = mapped_column(Date)
    page_type: Mapped[str] = mapped_column(Text)
    fantasypros_id: Mapped[int] = mapped_column(Integer)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))
    player_name: Mapped[str] = mapped_column(Text)
    position: Mapped[str] = mapped_column(Text)
    team_abbr: Mapped[str | None] = mapped_column(Text)
    ecr: Mapped[float] = mapped_column(Float)
    sd: Mapped[float | None] = mapped_column(Float)
    best: Mapped[int | None] = mapped_column(Integer)
    worst: Mapped[int | None] = mapped_column(Integer)
