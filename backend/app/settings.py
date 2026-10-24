"""Application settings, loaded from the environment and `.env`.

Defaults mirror the parameter table in CLAUDE.md; change them there and here together.
"""

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class PollTier(BaseModel):
    """Poll every `interval_minutes` while kickoff is at least `min_hours_to_kickoff` away."""

    min_hours_to_kickoff: float
    interval_minutes: int


DEFAULT_POLL_TIERS = [
    PollTier(min_hours_to_kickoff=72, interval_minutes=12 * 60),
    PollTier(min_hours_to_kickoff=24, interval_minutes=3 * 60),
    PollTier(min_hours_to_kickoff=3, interval_minutes=60),
    PollTier(min_hours_to_kickoff=0, interval_minutes=15),
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://localhost:5432/propline"
    test_database_url: str = "postgresql+psycopg://localhost:5432/propline_test"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    odds_provider: Literal["the_odds_api"] = "the_odds_api"
    odds_api_key: SecretStr | None = None
    odds_api_base_url: str = "https://api.the-odds-api.com"
    odds_api_regions: str = "us"
    odds_api_timeout_seconds: float = 10.0

    # Sized for The Odds API free plan (500 credits/month); raise it on a paid plan.
    daily_credit_budget: int = 15
    poll_tiers: list[PollTier] = Field(default_factory=lambda: list(DEFAULT_POLL_TIERS))
    stale_quote_minutes: int = 90
    fuzzy_match_threshold: int = 90
    steam_min_books: int = 3
    steam_window_min: int = 10
    stale_line_prob_pts: float = 5.0
    stale_line_min: int = 30
    context_lookback_hours: int = 24
    alert_threshold_pts: float = 2.0
    alert_digest_min: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()
