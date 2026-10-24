import json
from pathlib import Path

import pytest

from app.distribution_params import (
    DEFAULT_PATH,
    DistributionParams,
    load_distribution_params,
)
from app.settings import Settings
from db.seed_data import MARKETS
from engine.distributions import StatModel, fit_line

MAIN_STATS = {m["stat"] for m in MARKETS if not m["is_alternate"]}
POSITIONS = ("QB", "RB", "WR", "TE")


@pytest.fixture(scope="module")
def shipped() -> DistributionParams:
    return load_distribution_params()


def write(tmp_path: Path, **changes: object) -> Path:
    data: dict[str, object] = {
        "version": 1,
        "source": "test",
        "generated": "2026-10-24",
        "stats": {
            "rec_yds": {
                "WR": {"family": "gamma", "shape": 2.5},
                "default": {"family": "gamma", "shape": 2.5},
            }
        },
    }
    data.update(changes)
    path = tmp_path / "params.json"
    path.write_text(json.dumps(data))
    return path


# The shipped file ------------------------------------------------------------------------------


def test_the_shipped_file_covers_every_seeded_stat_for_every_position(
    shipped: DistributionParams,
) -> None:
    assert DEFAULT_PATH.exists()
    for stat in MAIN_STATS:
        for position in (*POSITIONS, None, "K", "HB", "FB"):
            assert shipped.model(stat, position) is not None, (stat, position)


def test_the_seeded_market_families_match_the_fitted_ones(shipped: DistributionParams) -> None:
    """`markets.distribution_family` is the family a market is described by; keep it in step."""
    for market in MARKETS:
        model = shipped.model(market["stat"], None)
        assert model is not None
        assert model.family == market["distribution_family"], market["key"]


def test_the_shipped_shapes_are_the_measured_ones(shipped: DistributionParams) -> None:
    quarterback = shipped.model("pass_yds", "QB")
    assert quarterback is not None and quarterback.family == "gamma"
    assert quarterback.shape == pytest.approx(10.84)
    completions = shipped.model("pass_completions", "QB")
    assert completions is not None and completions.family == "negbin"
    assert completions.shape == pytest.approx(51.44)
    receptions = shipped.model("receptions", "WR")
    assert receptions == StatModel("poisson")
    assert shipped.model("anytime_td", "RB") == StatModel("bernoulli")


def test_positions_have_their_own_shapes_and_unknown_ones_fall_back(
    shipped: DistributionParams,
) -> None:
    wr, te, rb = (shipped.model("rec_yds", p) for p in ("WR", "TE", "RB"))
    assert wr and te and rb
    assert len({wr.shape, te.shape, rb.shape}) == 3
    assert shipped.model("rec_yds", "K") == wr == shipped.model("rec_yds", None)
    assert (
        shipped.model("rush_yds", "HB")
        == shipped.model("rush_yds", "FB")
        == shipped.model("rush_yds", "RB")
    )
    assert shipped.model("not_a_stat", "QB") is None


def test_low_volume_yardage_has_zero_games_built_in(shipped: DistributionParams) -> None:
    receiver = shipped.model("rec_yds", "WR")
    assert receiver is not None and receiver.zero_inflation is not None
    table = receiver.zero_inflation
    assert table.at(8) > 0.10 > table.at(80)  # many zeros at a low line, few at a high one
    quarterback = shipped.model("pass_yds", "QB")
    assert quarterback is not None and quarterback.zero_inflation is None


def test_every_shipped_model_can_be_fitted_at_a_typical_line(shipped: DistributionParams) -> None:
    typical = {"pass_yds": 250.5, "rush_yds": 55.5, "rec_yds": 45.5, "pass_tds": 1.5,
               "pass_completions": 22.5, "interceptions": 0.5, "rush_attempts": 14.5,
               "receptions": 4.5, "anytime_td": 0.5}  # fmt: skip
    for stat, by_position in shipped.models.items():
        for position, model in by_position.items():
            fitted = fit_line(model, typical[stat], 0.5)
            assert fitted.p_over(typical[stat]) == pytest.approx(0.5, abs=1e-6), (stat, position)
            assert fitted.mean > 0


# Bad files -------------------------------------------------------------------------------------


def test_a_good_file_loads(tmp_path: Path) -> None:
    params = load_distribution_params(write(tmp_path))
    assert params.model("rec_yds", "WR") == StatModel("gamma", 2.5)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"source": None},
        {"surprise": True},
        {"stats": {"rec_yds": {"WR": {"family": "weibull", "shape": 2.0}}}},
        {"stats": {"rec_yds": {"WR": {"family": "gamma"}}}},  # a gamma needs a shape
        {"stats": {"rec_yds": {"WR": {"family": "gamma", "shape": -1}}}},
        {"stats": {"rec_yds": {"WR": {"family": "poisson", "shape": 2}}}},
        {"stats": {"rec_yds": {"WR": {"family": "gamma", "shape": 2, "colour": "red"}}}},
        {"stats": {"rec_yds": {"WR": {"family": "gamma", "shape": 2, "zero_inflation":
                                      {"lines": [20, 10], "shares": [0.1, 0.2]}}}}},
        {"stats": "none"},
    ],
)  # fmt: skip
def test_a_malformed_file_is_rejected_by_name(tmp_path: Path, changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"params\.json is not valid"):
        load_distribution_params(write(tmp_path, **changes))


def test_garbage_and_missing_files(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(ValueError, match=r"broken\.json is not valid"):
        load_distribution_params(broken)
    with pytest.raises(FileNotFoundError):
        load_distribution_params(tmp_path / "missing.json")


# Settings --------------------------------------------------------------------------------------


def test_the_path_can_be_overridden_in_settings(tmp_path: Path) -> None:
    assert Settings(_env_file=None).distribution_params_path is None
    custom = write(tmp_path)
    assert (
        Settings(_env_file=None, distribution_params_path=custom).distribution_params_path == custom
    )
