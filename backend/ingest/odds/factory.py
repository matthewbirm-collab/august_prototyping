"""Build the configured odds provider."""

from app.settings import Settings
from ingest.odds.provider import OddsProvider
from ingest.odds.the_odds_api import TheOddsApiProvider


def get_odds_provider(settings: Settings, *, used_today: int = 0) -> OddsProvider:
    """`used_today` carries today's credit spend across processes (e.g. from ingestion_runs)."""
    match settings.odds_provider:
        case "the_odds_api":
            if settings.odds_api_key is None or not settings.odds_api_key.get_secret_value():
                raise ValueError("ODDS_API_KEY is not set")
            return TheOddsApiProvider(
                api_key=settings.odds_api_key.get_secret_value(),
                daily_credit_budget=settings.daily_credit_budget,
                regions=settings.odds_api_regions,
                base_url=settings.odds_api_base_url,
                timeout_seconds=settings.odds_api_timeout_seconds,
                used_today=used_today,
            )
