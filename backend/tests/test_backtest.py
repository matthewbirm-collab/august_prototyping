import csv
from pathlib import Path

import pytest

from analytics.backtest import (
    Graded,
    baseline_from,
    evaluate,
    gate,
    load_baseline,
    regressions,
    save_baseline,
    weekly_spearman,
    write_reports,
)


def row(
    projected: float,
    actual: float,
    position: str = "RB",
    week: int = 1,
    label: str = "close",
    ecr: float | None = None,
    p20: float | None = None,
    p80: float | None = None,
    player: int = 1,
) -> Graded:
    return Graded(
        season=2024,
        week=week,
        game_id="g",
        label=label,
        player_id=player,
        position=position,
        projected=projected,
        p20=projected - 5 if p20 is None else p20,
        p80=projected + 5 if p80 is None else p80,
        actual=actual,
        ecr=ecr,
    )


def test_errors_match_hand_calculations() -> None:
    [report] = evaluate([row(10, 12), row(10, 6), row(20, 20)]).values()
    assert report.n == 3
    assert report.mae == pytest.approx((2 + 4 + 0) / 3)
    assert report.rmse == pytest.approx(((4 + 16 + 0) / 3) ** 0.5)
    assert report.bias == pytest.approx((-2 + 4 + 0) / 3)


def test_coverage_counts_outcomes_below_each_end_of_the_range() -> None:
    rows = [row(10, a, p20=5, p80=15) for a in (2, 8, 12, 14, 20)]
    [report] = evaluate(rows).values()
    assert report.below_p20 == pytest.approx(1 / 5)
    assert report.below_p80 == pytest.approx(4 / 5)


def test_head_to_head_uses_only_players_ecr_ranked() -> None:
    rows = [row(10, 12, ecr=16), row(10, 6, ecr=7), row(30, 0)]  # the last has no ECR
    [report] = evaluate(rows).values()
    assert report.head_to_head == 2
    assert report.mae_ours == pytest.approx(3.0)
    assert report.mae_ecr == pytest.approx(2.5)
    assert report.beats_ecr is False
    assert evaluate([row(10, 12)])[("close", "RB")].beats_ecr is None


def test_rank_correlation_is_per_week_and_averaged() -> None:
    perfect = [row(p, p, week=1, player=p) for p in range(1, 7)]
    backwards = [row(p, -p, week=2, player=p) for p in range(1, 7)]
    assert weekly_spearman(perfect) == pytest.approx(1.0)
    assert weekly_spearman(perfect + backwards) == pytest.approx(0.0)
    assert weekly_spearman(perfect[:4]) is None  # too few in the week


def test_reports_are_per_label_and_position_in_order() -> None:
    rows = [row(1, 1, "TE"), row(1, 1, "QB"), row(1, 1, "RB", label="t1h"), row(1, 1, "WR")]
    assert list(evaluate(rows)) == [
        ("close", "QB"),
        ("close", "WR"),
        ("close", "TE"),
        ("t1h", "RB"),
    ]


def test_the_gate_needs_a_win_at_every_position() -> None:
    rows = [row(10, 10, "QB", ecr=12), row(10, 14, "RB", ecr=13), row(10, 10, "WR", label="t1h")]
    assert gate(evaluate(rows), "close") == {"QB": True, "RB": False}


def test_a_regression_beyond_five_percent_is_reported() -> None:
    reports = evaluate([row(10, 14)])  # MAE 4
    assert regressions(reports, {"close": {"RB": 3.85}}) == []  # 3.9% worse
    [message] = regressions(reports, {"close": {"RB": 3.7}})
    assert "close RB" in message and "+8.1%" in message
    assert regressions(reports, {}) == []
    assert baseline_from(reports) == {"close": {"RB": 4.0}}


def test_baselines_are_kept_per_run_kind(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    assert load_baseline(path, "market/ppr") == {}
    save_baseline(path, "market/ppr", {"close": {"RB": 4.0}})
    save_baseline(path, "model/ppr", {"close": {"RB": 5.0}})
    assert load_baseline(path, "market/ppr") == {"close": {"RB": 4.0}}
    assert load_baseline(path, "model/ppr") == {"close": {"RB": 5.0}}


def test_reports_are_written(tmp_path: Path) -> None:
    rows = [row(p, p + 1, player=p, ecr=p + 3) for p in range(1, 8)]
    rows += [row(p, p, "WR", player=10 + p) for p in range(1, 8)]
    reports = evaluate(rows)
    written = write_reports(rows, reports, tmp_path / "bt", title="Backtest: test", notes=["hi"])
    assert {p.name for p in written} == {
        "by_position.csv",
        "by_week.csv",
        "coverage.png",
        "summary.md",
    }
    with (tmp_path / "bt" / "by_position.csv").open() as handle:
        found = {r["position"]: r for r in csv.DictReader(handle)}
    assert found["RB"]["beats_ecr"] == "True" and found["WR"]["beats_ecr"] == ""
    text = (tmp_path / "bt" / "summary.md").read_text()
    assert "# Backtest: test" in text and "- hi" in text
    assert "- RB: beats ECR" in text and "- WR: no ECR to compare" in text
