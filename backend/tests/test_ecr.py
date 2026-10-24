import json
from pathlib import Path

import pytest

from analytics.ecr import EcrCurves, fit_curve, load_curves, position_ranks


def test_ranks_order_by_ecr_with_ties_broken_by_id() -> None:
    assert position_ranks({10: 3.5, 11: 1.2, 12: 3.5, 13: 2.0}) == {11: 1, 13: 2, 10: 3, 12: 4}
    assert position_ranks({}) == {}


def test_the_curve_is_mean_points_by_rank_made_non_increasing() -> None:
    pairs = [(1, 20.0), (1, 24.0), (2, 15.0), (3, 18.0), (4, 9.0)]
    curve = fit_curve(pairs, 5)
    assert curve[0] == 22.0
    assert curve[1] == curve[2] == pytest.approx(16.5)  # 15 then 18 pooled
    assert curve[3] == 9.0
    assert curve[4] == 9.0  # never seen: the rank above
    assert curve == sorted(curve, reverse=True)


def test_ranks_past_the_cap_pool_into_the_last_one() -> None:
    curve = fit_curve([(1, 10.0), (2, 6.0), (50, 2.0), (90, 4.0)], 3)
    assert len(curve) == 3
    assert curve[2] == pytest.approx(3.0)


def test_points_look_up_by_season_scoring_and_position() -> None:
    curves = EcrCurves({"2024": {"ppr": {"RB": [20.0, 15.0, 10.0]}}})
    assert curves.points(2024, "ppr", "RB", 2) == 15.0
    assert curves.points(2024, "ppr", "RB", 9) == 10.0  # past the curve: its last value
    assert curves.points(2024, "ppr", "WR", 1) is None
    assert curves.points(2023, "ppr", "RB", 1) is None
    assert curves.points(2024, "ppr", "RB", 0) is None
    assert curves.seasons() == {2024}


def test_load_reads_the_file_and_tolerates_none(tmp_path: Path) -> None:
    assert load_curves(tmp_path / "missing.json").curves == {}
    path = tmp_path / "curve.json"
    path.write_text(json.dumps({"curves": {"2025": {"ppr": {"QB": [25.0]}}}}))
    assert load_curves(path).points(2025, "ppr", "QB", 1) == 25.0


def test_the_shipped_curves_only_use_earlier_seasons() -> None:
    shipped = json.loads(
        (Path(__file__).resolve().parents[1] / "config" / "ecr_points_curve.json").read_text()
    )
    assert "before" in shipped["note"]
    curves = load_curves()
    for season in (2023, 2024, 2025):
        for position in ("QB", "RB", "WR", "TE"):
            curve = curves.curves[str(season)]["ppr"][position]
            assert curve == sorted(curve, reverse=True)
            assert 10 < curve[0] < 35
