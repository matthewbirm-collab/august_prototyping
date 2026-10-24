from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from ingest import backfill_plan as plan

KICKOFF = datetime(2025, 10, 19, 17, 0, tzinfo=UTC)
MARKETS9 = tuple(f"m{i}" for i in range(9))


def hours_before(probe: plan.Probe) -> float:
    return (KICKOFF - probe.at).total_seconds() / 3600


def labelled(probes: list[plan.Probe]) -> dict[str, float]:
    return {label: hours_before(p) for p in probes for label in p.labels}


# Probes ----------------------------------------------------------------------------------------


def test_the_fixed_snapshots_sit_at_their_offsets() -> None:
    probes = plan.sweep(KICKOFF, set(plan.LABELS))
    assert labelled(probes) == {"t72h": 72, "t24h": 24, "t6h": 6, "t1h": 1}


def test_probes_are_in_time_order_and_the_open_search_covers_nine_days_to_one_hour_out() -> None:
    probes = plan.sweep(KICKOFF, set(plan.LABELS))
    times = [p.at for p in probes]
    assert times == sorted(times)
    assert len(set(times)) == len(times)
    scans = [p for p in probes if p.scan]
    assert hours_before(scans[0]) == 216  # nine days out
    assert all(hours_before(p) > 1 for p in scans)
    assert {hours_before(b) - hours_before(a) for a, b in pairwise(scans)} == {-6}


def test_a_scan_point_that_lands_on_a_fixed_snapshot_is_one_probe() -> None:
    probes = {hours_before(p): p for p in plan.sweep(KICKOFF, set(plan.LABELS))}
    assert probes[72].scan and probes[72].labels == ("t72h",)
    assert probes[24].scan and probes[24].labels == ("t24h",)
    assert not probes[1].scan and probes[1].labels == ("t1h",)  # one hour out is off the grid


def test_without_open_there_is_no_search() -> None:
    probes = plan.sweep(KICKOFF, {"t24h", "t1h", "close"})
    assert labelled(probes) == {"t24h": 24, "t1h": 1}
    assert not any(p.scan for p in probes)


def test_only_pending_labels_are_probed() -> None:
    assert labelled(plan.sweep(KICKOFF, {"t6h"})) == {"t6h": 6}
    assert plan.sweep(KICKOFF, {"close"}) == []
    assert plan.sweep(KICKOFF, set()) == []


def test_the_scan_step_is_adjustable() -> None:
    coarse = plan.sweep(KICKOFF, {"open"}, scan_step=timedelta(hours=12))
    fine = plan.sweep(KICKOFF, {"open"}, scan_step=timedelta(hours=3))
    assert len(fine) > len(coarse)


def test_close_is_tried_just_before_kickoff_then_earlier() -> None:
    minutes = [(KICKOFF - at).total_seconds() / 60 for at in plan.close_candidates(KICKOFF)]
    assert minutes == [1, 10, 30]


# Cost ------------------------------------------------------------------------------------------


def games(count: int, season: int = 2025) -> list[plan.GameInfo]:
    return [plan.GameInfo(f"{season}_{i}", season, 1 + i % 18, KICKOFF, True) for i in range(count)]


def test_a_call_costs_ten_credits_per_market_per_region() -> None:
    assert plan.credits_per_call(MARKETS9, 1) == 90
    assert plan.credits_per_call(MARKETS9, 2) == 180
    assert plan.credits_per_call((*MARKETS9, "a", "b", "c", "d", "e"), 1) == 140


def test_the_full_backfill_matches_the_quoted_figure() -> None:
    every_game = games(272, 2023) + games(272, 2024) + games(272, 2025)
    estimate = plan.estimate_backfill(every_game, set(), plan.LABELS, 90, unlinked_weeks=54)

    assert estimate.games == 816
    assert estimate.snapshots == estimate.calls == 4896
    assert estimate.snapshot_credits == 440_640
    assert estimate.credits == 440_640 + 54
    assert [s.credits for s in estimate.seasons] == [146_880] * 3
    assert estimate.credits_per_call == 90


def test_fewer_snapshots_or_markets_cost_proportionally_less() -> None:
    every_game = games(816)
    three = plan.estimate_backfill(every_game, set(), ("t24h", "t1h", "close"), 90)
    assert three.snapshot_credits == 220_320
    core = plan.estimate_backfill(every_game, set(), plan.LABELS, 70)
    assert core.snapshot_credits == 342_720


def test_finished_snapshots_are_not_counted() -> None:
    every_game = games(10)
    done = {(g.game_id, label) for g in every_game for label in plan.LABELS}
    assert plan.estimate_backfill(every_game, done, plan.LABELS, 90).calls == 0

    some = {(every_game[0].game_id, "open"), (every_game[0].game_id, "close")}
    estimate = plan.estimate_backfill(every_game, some, plan.LABELS, 90)
    assert estimate.done == 2
    assert estimate.calls == 10 * 6 - 2
    assert estimate.snapshot_credits == (10 * 6 - 2) * 90


def test_linking_costs_one_credit_per_week_and_up_to_one_per_game_more() -> None:
    estimate = plan.estimate_backfill(
        games(5), set(), plan.LABELS, 90, unlinked_weeks=3, unlinked_games=5
    )
    assert (estimate.link_credits, estimate.link_credits_worst) == (3, 8)
    assert estimate.credits == estimate.snapshot_credits + 8


@pytest.mark.parametrize(("labels", "extra"), [(plan.LABELS, 16), (("t24h", "t1h", "close"), 0)])
def test_typical_requests_include_the_open_search_only_when_open_is_wanted(
    labels: tuple[str, ...], extra: int
) -> None:
    estimate = plan.estimate_backfill(games(1), set(), labels, 90)
    assert estimate.typical_requests == len(labels) + extra


def test_the_estimate_prints_a_table_and_the_total() -> None:
    estimate = plan.estimate_backfill(games(272, 2023) + games(272, 2024), set(), plan.LABELS, 90)
    text = plan.format_estimate(estimate, max_rps=2)
    assert "544 games x 6 snapshots" in text
    assert "293,760" in text  # 544 x 6 x 90
    assert "2023" in text and "2024" in text
    assert "at 2 requests a second" in text
