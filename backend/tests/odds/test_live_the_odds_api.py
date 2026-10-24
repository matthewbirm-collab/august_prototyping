"""Live checks against The Odds API. Run with `uv run pytest -m live` once ODDS_API_KEY is set.

Only free calls are made (listing events costs no credits).
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.settings import Settings
from ingest.odds.factory import get_odds_provider

pytestmark = pytest.mark.live


@pytest.fixture
def settings() -> Settings:
    settings = Settings()
    if settings.odds_api_key is None:
        pytest.skip("ODDS_API_KEY is not set")
    return settings


def test_list_upcoming_events(settings: Settings) -> None:
    provider = get_odds_provider(settings)
    now = datetime.now(UTC)
    events = provider.list_events(now, now + timedelta(days=8))

    assert all(event.event_id and event.home_team for event in events)
    assert provider.credit_usage().used_today == 0
