"""Behavioral checks for the experimental WR research model."""

from dataclasses import replace

import pytest

from engine.wr_model import Settings, Week, forecast, priors, validate_sample

WEIGHTS = {"reception": 0.5, "receiving_yards": 0.1, "rushing_yards": 0.1, "total_tds": 6.0}


def sample() -> list[Week]:
    return [
        Week("Example", "example", "SEA", w, 30, 40, 9, 6, 75, 90, 0, 0, 0) for w in [3, 4, 5, 6]
    ]


def test_bad_counts_duplicates_and_future_data_fail() -> None:
    rows = sample()
    with pytest.raises(ValueError, match="opportunity"):
        replace(rows[0], receptions=10)
    with pytest.raises(ValueError, match="Duplicate"):
        validate_sample([*rows, rows[0]], Settings())
    with pytest.raises(ValueError, match="future"):
        validate_sample([replace(rows[0], week=7)], Settings())


def test_opportunity_scales_without_double_counting_yards() -> None:
    rows = sample()
    cfg = Settings()
    pool = priors(rows, cfg)
    base = forecast(rows, pool, 30, 0, WEIGHTS, cfg, simulate=False)
    higher = forecast(rows, pool, 45, 0, WEIGHTS, cfg, simulate=False)
    assert higher["mean"] == pytest.approx(base["mean"] * 1.5)
    assert base["projected_receptions"] <= base["projected_targets"] <= base["projected_routes"]
    assert base["mean"] == pytest.approx(
        0.5 * base["projected_receptions"] + 0.1 * base["projected_receiving_yards"]
    )


def test_td_added_once_and_workload_scenario() -> None:
    rows = sample()
    cfg = Settings()
    pool = priors(rows, cfg)
    zero = forecast(rows, pool, 35, 0, WEIGHTS, cfg, simulate=False)
    td = forecast(rows, pool, 35, 0.5, WEIGHTS, cfg, simulate=False)
    low = forecast(rows, pool, 35, 0.5, WEIGHTS, cfg, workload=0.75, simulate=False)
    assert td["mean"] - zero["mean"] == pytest.approx(3)
    assert low["mean"] == pytest.approx(td["mean"] * 0.75)


def test_recent_role_matters_but_does_not_change_observed_metrics() -> None:
    rows = sample()
    rows[-1] = replace(rows[-1], routes=40)
    cfg = Settings()
    pool = priors(rows, cfg)
    latest = forecast(
        rows, pool, 35, 0, WEIGHTS, replace(cfg, role_latest_weight=1), simulate=False
    )
    history = forecast(
        rows, pool, 35, 0, WEIGHTS, replace(cfg, role_latest_weight=0), simulate=False
    )
    assert latest["projected_routes"] > history["projected_routes"]
    assert latest["observed_yprr"] == history["observed_yprr"]


def test_simulated_mean_and_reproducibility() -> None:
    rows = sample()
    cfg = Settings(draws=12000)
    pool = priors(rows, cfg)
    a = forecast(rows, pool, 35, 0.4, WEIGHTS, cfg)
    assert a == forecast(rows, pool, 35, 0.4, WEIGHTS, cfg)
    assert a["p20"] < a["p80"]
    assert abs(a["simulation_mean"] - a["mean"]) < 4 * a["sd"] / cfg.draws**0.5


def test_configuration_and_scoring_validation() -> None:
    with pytest.raises(ValueError):
        Settings(target_prior_exposure=0)
    with pytest.raises(ValueError):
        Settings(role_latest_weight=2)
    cfg = Settings()
    rows = sample()
    with pytest.raises(ValueError, match="scoring"):
        forecast(rows, priors(rows, cfg), 35, 0.3, {}, cfg)


def test_receiver_td_normalization_keeps_qb_default() -> None:
    from datetime import UTC, datetime

    from ingest.propline_qb import normalize

    now = datetime(2026, 10, 24, tzinfo=UTC)
    snapshot = {
        "captured_at": now.isoformat(),
        "events": [
            {
                "id": "game",
                "away_team": "A",
                "home_team": "B",
                "commence_time": "2026-10-25T17:00:00+00:00",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "player_anytime_td",
                                "last_update": now.isoformat(),
                                "outcomes": [
                                    {"description": "Receiver", "name": "Yes", "price": 200}
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    assert normalize(snapshot, now, 90, 0.9)[0] == {}
    values, _ = normalize(snapshot, now, 90, 0.9, quarterbacks_only=False)
    assert values["game:name:receiver"]["player_anytime_td"][0].probability == pytest.approx(0.3)


def test_frozen_report_filters_availability_and_rejects_false_asof() -> None:
    import json
    import runpy
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    build = runpy.run_path(str(root / "scripts/wr_model.py"))["build"]
    config = json.loads((root / "scoring_configs/wr_half_ppr.json").read_text())
    config["model"]["draws"] = 100
    data = root / "docs/research/2026-week-7/wr-model"
    with pytest.raises(ValueError, match="as_of"):
        build(data, config, "2026-10-20T00:00:00Z")
    report = build(data, config)
    ranked = {p["player"]: p for p in report["rankings"]}
    excluded = {p["player"]: p["reason"] for p in report["excluded"]}
    assert excluded["Emeka Egbuka"] == "out"
    assert excluded["Davante Adams"] == "out"
    assert excluded["Khalil Shakir"] == "bye"
    assert excluded["Rome Odunze"] == "locked"
    assert ranked["DeVonta Smith"]["availability"] == "conditional"
    assert ranked["Tee Higgins"]["observed_yprr"] == pytest.approx(269 / 108)
    assert ranked["Amon-Ra St. Brown"]["observed_route_participation"] == pytest.approx(110 / 122)
