import csv
import math
from pathlib import Path

import numpy as np
import pytest

from analytics.calibration import (
    FLAG_Z,
    MIN_OBSERVATIONS,
    Bucket,
    Observation,
    RangeCheck,
    brier_score,
    evaluate,
    log_loss,
    range_check,
    reliability,
    went_over,
    write_reports,
)
from engine.distributions import StatModel, ZeroInflation, fit_anytime_td, fit_line

RUSH = "player_rush_yds"
RECEPTIONS = "player_receptions"
TD = "player_anytime_td"
GAMMA = StatModel("gamma", 3.0, ZeroInflation((10.0, 40.0), (0.15, 0.02)))
POISSON = StatModel("poisson")


def obs(p: float, over: bool, market: str = RUSH, line: float = 60.5) -> Observation:
    return Observation(market, p, line, line + 10 if over else line - 10)


def calibrated(n: int, seed: int = 1, bias: float = 0.0) -> list[Observation]:
    """Props whose outcome is really 'over' with probability p (less `bias`)."""
    rng = np.random.default_rng(seed)
    probabilities = rng.uniform(0.1, 0.9, n)
    return [obs(float(p), bool(rng.random() < p - bias)) for p in probabilities]


def drawn_from_the_fit(
    n: int, scale: float = 1.0, seed: int = 2, shape: StatModel = GAMMA
) -> list[Observation]:
    """Outcomes drawn from the distribution fitted to each line, optionally stretched by `scale`."""
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        line, p = float(rng.uniform(20, 120)) // 1 + 0.5, float(rng.uniform(0.3, 0.7))
        fitted = fit_line(shape, line, p)
        draw = float(fitted.sample(1, rng)[0]) * scale
        out.append(Observation(RUSH, p, line, draw, shape))
    return out


# Outcomes --------------------------------------------------------------------------------------


def test_over_under_and_push() -> None:
    assert went_over(Observation(RUSH, 0.5, 60.5, 61)) is True
    assert went_over(Observation(RUSH, 0.5, 60.5, 60)) is False
    assert went_over(Observation(RECEPTIONS, 0.5, 5.0, 5)) is None  # a push


def test_anytime_td_goes_over_with_at_least_one_touchdown() -> None:
    assert went_over(Observation(TD, 0.4, None, 1)) is True
    assert went_over(Observation(TD, 0.4, None, 2)) is True
    assert went_over(Observation(TD, 0.4, None, 0)) is False


# Reliability -----------------------------------------------------------------------------------


def test_a_calibrated_feed_lands_on_the_diagonal() -> None:
    report = evaluate(calibrated(20_000))[RUSH]

    assert report.calibration_error < 0.012
    assert report.mean_predicted == pytest.approx(report.base_rate, abs=0.01)
    assert report.flagged_buckets <= 2
    assert report.enough_data
    assert all(abs(b.predicted - b.observed) < 0.04 for b in report.buckets)


def test_a_biased_feed_is_flagged() -> None:
    """Props priced at p that really go over 10 points less often."""
    report = evaluate(calibrated(20_000, bias=0.10))[RUSH]
    assert report.calibration_error > 0.08
    assert report.flagged_buckets >= 8
    assert report.base_rate < report.mean_predicted - 0.08


def test_buckets_hold_equal_counts_in_increasing_probability() -> None:
    buckets = reliability(calibrated(1003), 10)
    assert len(buckets) == 10
    assert sum(b.n for b in buckets) == 1003
    assert {b.n for b in buckets} <= {100, 101}
    assert [b.predicted for b in buckets] == sorted(b.predicted for b in buckets)
    assert all(b.low <= b.predicted <= b.high for b in buckets)


def test_fewer_props_than_buckets_still_work() -> None:
    assert len(reliability([obs(0.4, True), obs(0.6, False), obs(0.5, True)], 10)) == 3
    assert reliability([], 10) == []


def test_a_bucket_knows_how_far_off_it_is_in_standard_errors() -> None:
    assert Bucket(0.5, 0.5, 100, 0.5, 0.5).z == 0
    assert Bucket(0.5, 0.5, 100, 0.5, 0.60).z == pytest.approx(2.0)  # sqrt(.25 / 100) = .05
    assert Bucket(0.5, 0.5, 400, 0.5, 0.60).z == pytest.approx(4.0)
    assert FLAG_Z > 2


def test_brier_and_log_loss_match_hand_calculations() -> None:
    rows = [obs(0.8, True)] * 4 + [obs(0.8, False)]
    assert brier_score(rows) == pytest.approx((4 * 0.04 + 0.64) / 5)
    assert log_loss(rows) == pytest.approx(-(4 * math.log(0.8) + math.log(0.2)) / 5)
    assert brier_score([obs(0.5, True), obs(0.5, False)]) == 0.25
    # A confident miss is punished far harder by log-loss than by Brier.
    assert log_loss([obs(0.999999, False)]) > 10


def test_pushes_are_counted_but_not_scored() -> None:
    rows = [
        obs(0.6, True, RECEPTIONS, 5.0),
        obs(0.6, False, RECEPTIONS, 5.0),
        Observation(RECEPTIONS, 0.6, 5.0, 5),
        Observation(RECEPTIONS, 0.6, 5.0, 5),
    ]
    report = evaluate(rows)[RECEPTIONS]
    assert (report.n, report.pushes) == (2, 2)
    assert report.base_rate == 0.5


def test_markets_are_reported_separately_and_empty_ones_dropped() -> None:
    rows = [*calibrated(200), obs(0.5, True, RECEPTIONS, 4.5), Observation(TD, 0.4, None, 0)]
    reports = evaluate(rows)
    assert list(reports) == [TD, RECEPTIONS, RUSH]  # alphabetical
    assert evaluate([Observation(RECEPTIONS, 0.5, 5.0, 5)]) == {}
    assert reports[RUSH].enough_data and not reports[RECEPTIONS].enough_data
    assert MIN_OBSERVATIONS > 1


# The fitted ranges -----------------------------------------------------------------------------


def test_outcomes_from_the_fitted_distribution_fall_where_they_should() -> None:
    check = range_check(drawn_from_the_fit(6000))
    assert check is not None
    assert check.below_p20 == pytest.approx(0.2, abs=0.025)
    assert check.inside_p20_p80 == pytest.approx(0.6, abs=0.03)
    assert check.above_p80 == pytest.approx(0.2, abs=0.025)
    assert check.mean_position == pytest.approx(0.5, abs=0.02)
    assert check.verdict == "ranges about right"


def test_outcomes_more_spread_than_the_fit_say_the_ranges_are_too_narrow() -> None:
    wide = range_check(
        [
            Observation(RUSH, o.p_over, o.line, o.actual * s, o.model)
            for o, s in zip(
                drawn_from_the_fit(6000),
                np.random.default_rng(3).choice([0.4, 2.2], 6000),
                strict=True,
            )
        ]
    )
    assert wide is not None
    assert wide.inside_p20_p80 < 0.5
    assert wide.verdict == "ranges too narrow"


def test_outcomes_tighter_than_the_fit_say_the_ranges_are_too_wide() -> None:
    narrow = []
    for o in drawn_from_the_fit(6000):
        fitted = fit_line(GAMMA, o.line or 0, o.p_over)
        narrow.append(
            Observation(
                RUSH, o.p_over, o.line, fitted.quantile(0.5) * 0.98 + 0.02 * o.actual, o.model
            )
        )
    check = range_check(narrow)
    assert check is not None
    assert check.inside_p20_p80 > 0.9
    assert check.verdict == "ranges too wide"


def test_a_count_market_is_checked_with_its_mid_pit() -> None:
    rng = np.random.default_rng(4)
    rows = []
    for _ in range(6000):
        line, p = float(rng.integers(2, 9)) + 0.5, float(rng.uniform(0.3, 0.7))
        rows.append(
            Observation(
                RECEPTIONS, p, line, float(fit_line(POISSON, line, p).sample(1, rng)[0]), POISSON
            )
        )
    check = range_check(rows)
    assert check is not None
    assert check.mean_position == pytest.approx(0.5, abs=0.02)
    assert check.inside_p20_p80 == pytest.approx(0.6, abs=0.04)


def test_anytime_td_outcomes_use_the_poisson_rate() -> None:
    rng = np.random.default_rng(6)
    rows = []
    for _ in range(6000):
        p = float(rng.uniform(0.1, 0.7))
        rows.append(
            Observation(
                TD, p, None, float(fit_anytime_td(p).sample(1, rng)[0]), StatModel("bernoulli")
            )
        )
    report = evaluate(rows)[TD]
    assert report.calibration_error < 0.03
    assert report.range_check is not None
    assert report.range_check.mean_position == pytest.approx(0.5, abs=0.03)


def test_no_model_means_no_range_check() -> None:
    assert range_check([obs(0.5, True)]) is None
    assert evaluate(calibrated(100))[RUSH].range_check is None


def test_the_verdict_thresholds() -> None:
    def verdict(inside: float) -> str:
        return RangeCheck(100, (1 - inside) / 2, inside, (1 - inside) / 2, 0.5).verdict

    assert [verdict(x) for x in (0.40, 0.549, 0.60, 0.651, 0.80)] == [
        "ranges too narrow",
        "ranges too narrow",
        "ranges about right",
        "ranges too wide",
        "ranges too wide",
    ]


# The files -------------------------------------------------------------------------------------


def test_reports_write_a_csv_and_a_plot_per_market_and_summaries(tmp_path: Path) -> None:
    reports = evaluate([*drawn_from_the_fit(400), *calibrated(300, seed=9)])
    reports.update(
        evaluate([obs(0.5, True, RECEPTIONS, 4.5)] * 5 + [obs(0.4, False, RECEPTIONS, 4.5)] * 5)
    )
    out = tmp_path / "nested" / "calibration"
    written = write_reports(reports, out)

    names = {p.name for p in written}
    assert names == {f"{m}.{ext}" for m in reports for ext in ("csv", "png")} | {
        "summary.csv",
        "summary.md",
    }
    for path in written:
        assert path.stat().st_size > 0
    for png in out.glob("*.png"):
        assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    with (out / f"{RUSH}.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert [r["n"] for r in rows] and sum(int(r["n"]) for r in rows) == reports[RUSH].n
    assert set(rows[0]) == {"low", "high", "n", "predicted", "observed", "z"}

    with (out / "summary.csv").open() as handle:
        summary = {r["market"]: r for r in csv.DictReader(handle)}
    assert set(summary) == set(reports)
    assert float(summary[RUSH]["brier"]) == pytest.approx(reports[RUSH].brier, abs=1e-4)
    assert summary[RECEPTIONS]["verdict"] == ""  # no model supplied for the range check

    text = (out / "summary.md").read_text()
    assert "# Calibration" in text and f"| {RUSH} |" in text
    assert "too few to judge" in text  # the 10-prop market
