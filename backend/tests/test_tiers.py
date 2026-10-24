from datetime import UTC, datetime, timedelta

import pytest

from app.settings import DEFAULT_POLL_TIERS
from ingest import tiers

NOW = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)
TIERS = DEFAULT_POLL_TIERS


def at(hours: float) -> datetime:
    """A kickoff `hours` from NOW."""
    return NOW + timedelta(hours=hours)


@pytest.mark.parametrize(
    ("hours", "interval"),
    [
        (200, 720),  # more than 72h: every 12h
        (72.01, 720),
        (72, 180),  # 72-24h: every 3h
        (24.01, 180),
        (24, 60),  # 24-3h: hourly
        (3.01, 60),
        (3, 15),  # under 3h: every 15 minutes
        (0.01, 15),
    ],
)
def test_tier_boundaries(hours: float, interval: int) -> None:
    tier = tiers.tier_for(hours, TIERS)
    assert tier is not None
    assert tier.interval_minutes == interval


@pytest.mark.parametrize("hours", [0, -0.5, -30])
def test_no_tier_at_or_after_kickoff(hours: float) -> None:
    assert tiers.tier_for(hours, TIERS) is None


def test_tier_order_in_settings_does_not_matter() -> None:
    shuffled = [TIERS[2], TIERS[0], TIERS[3], TIERS[1]]
    assert tiers.tier_for(10, shuffled) == tiers.tier_for(10, TIERS)


def test_closer_than_the_last_threshold_uses_the_fastest_tier() -> None:
    from app.settings import PollTier

    coarse = [PollTier(min_hours_to_kickoff=24, interval_minutes=60)]
    tier = tiers.tier_for(1, coarse)
    assert tier is not None
    assert tier.interval_minutes == 60


def test_never_polled_is_due_until_kickoff() -> None:
    assert tiers.is_due(NOW, at(5), None, TIERS)
    assert not tiers.is_due(NOW, NOW - timedelta(minutes=1), None, TIERS)


@pytest.mark.parametrize(
    ("kickoff_hours", "minutes_since_poll", "due"),
    [
        (2, 5, False),  # 15m tier, polled 5 minutes ago
        (2, 13, True),  # within the 2-minute slack
        (2, 12, False),
        (20, 50, False),  # hourly tier
        (20, 58, True),
        (20, 61, True),
        (60, 170, False),  # 3h tier
        (60, 178, True),
        (120, 11 * 60, False),  # 12h tier
        (120, 12 * 60, True),
    ],
)
def test_due_follows_the_tier_interval(
    kickoff_hours: float, minutes_since_poll: int, due: bool
) -> None:
    last = NOW - timedelta(minutes=minutes_since_poll)
    assert tiers.is_due(NOW, at(kickoff_hours), last, TIERS) is due


def test_slack_never_exceeds_a_quarter_of_the_interval() -> None:
    from app.settings import PollTier

    fast = [PollTier(min_hours_to_kickoff=0, interval_minutes=4)]
    last = NOW - timedelta(minutes=2)
    assert not tiers.is_due(NOW, at(1), last, fast)  # a 2-minute slack would say due
    assert tiers.is_due(NOW, at(1), NOW - timedelta(minutes=3, seconds=1), fast)


def test_count_polls_follows_the_tiers_down_to_kickoff() -> None:
    # 2h out and inside the 15-minute tier: 8 polls (2.0h, 1.75h, ... 0.25h).
    assert tiers.count_polls(NOW, at(2), TIERS) == 8
    assert tiers.count_polls(NOW, NOW - timedelta(hours=1), TIERS) == 0


def test_count_polls_matches_a_step_by_step_simulation() -> None:
    polls, hours = 0, 25.0
    while hours > 0:
        tier = tiers.tier_for(hours, TIERS)
        assert tier is not None
        polls += 1
        hours -= tier.interval_minutes / 60
    assert tiers.count_polls(NOW, at(25), TIERS) == polls


@pytest.mark.parametrize(
    ("minutes", "label"), [(720, "12h"), (180, "3h"), (60, "1h"), (15, "15m"), (90, "90m")]
)
def test_interval_labels_round_trip(minutes: int, label: str) -> None:
    assert tiers.interval_label(minutes) == label
    assert tiers.parse_interval(label) == minutes


@pytest.mark.parametrize("label", ["", "auto", "h", "0m", "-5m", "1d", "abc", "1.5h"])
def test_bad_tier_labels_are_rejected(label: str) -> None:
    with pytest.raises(ValueError, match="bad tier"):
        tiers.parse_interval(label)


def test_credit_cost_counts_markets_and_regions() -> None:
    markets = ["m1", "m2", "m3"]
    assert tiers.credits_per_poll(markets, "us", None) == 3
    assert tiers.credits_per_poll(markets, "us,us2", None) == 6
    # Every 10 bookmakers count as a region, and bookmakers replace regions.
    assert tiers.credits_per_poll(markets, "us,us2", [f"b{i}" for i in range(11)]) == 6
    assert tiers.credits_per_poll(markets, "us", ["draftkings"]) == 3


def test_projection_fits_games_in_kickoff_order() -> None:
    kickoffs = [at(120), at(2), at(2)]  # 8 + 8 + 12h-tier polls
    far_polls = tiers.count_polls(NOW, at(120), TIERS)
    projection = tiers.project_spend(
        kickoffs, NOW, TIERS, credits_per_poll=2, daily_budget=10, days=1
    )
    assert projection.games == 3
    assert projection.polls == 8 + 8 + far_polls
    assert projection.credits == 2 * projection.polls
    assert projection.budget == 10
    assert projection.games_within_budget == 0  # the first game alone costs 16

    roomy = tiers.project_spend(kickoffs, NOW, TIERS, 2, daily_budget=1000, days=1)
    assert roomy.games_within_budget == 3
    assert "Within budget" in tiers.format_projection(roomy, 1000)


def test_projection_text_reports_the_overrun() -> None:
    projection = tiers.project_spend([at(2)], NOW, TIERS, 9, daily_budget=15, days=7)
    text = tiers.format_projection(projection, 15)
    assert "1 games, 8 polls x 9 credits = 72 credits" in text
    assert "Budget: 15/day = 105 over 7 days" in text
    assert "Within budget" in text

    heavy = tiers.project_spend([at(2)] * 3, NOW, TIERS, 9, daily_budget=5, days=1)
    assert "Over budget by" in tiers.format_projection(heavy, 5)
