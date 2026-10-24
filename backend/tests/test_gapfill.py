import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine.distributions import StatModel, ZeroInflation
from engine.gapfill import (
    LEAGUE_IMPLIED_POINTS,
    PROMOTED_SHARE,
    PlayerHistory,
    Shares,
    TeamHistory,
    TeamVolume,
    distribution,
    fill,
    implied_points,
    shares,
    team_volume,
)
from engine.projection import MODEL_CREDIT, StatInput, project
from engine.scoring import standard_weights

PPR = standard_weights(1.0)
# Four games of a fairly average offence.
TEAM = TeamHistory(
    games=4, pass_attempts=132, rush_attempts=108, targets=124, pass_tds=6, rush_tds=4, rec_tds=6
)
WR1 = PlayerHistory(1, "WR", 4, targets=36, receptions=24, rec_yds=320, rec_tds=3, depth_rank=1)
WR2 = PlayerHistory(2, "WR", 4, targets=24, receptions=15, rec_yds=180, rec_tds=1, depth_rank=2)
WR3 = PlayerHistory(3, "WR", 4, targets=12, receptions=7, rec_yds=80, depth_rank=3)
TE1 = PlayerHistory(4, "TE", 4, targets=20, receptions=14, rec_yds=150, rec_tds=1, depth_rank=1)
RB1 = PlayerHistory(5, "RB", 4, targets=12, receptions=10, rec_yds=60, carries=64, rush_yds=280,
                    rush_tds=3, depth_rank=1)  # fmt: skip
RB2 = PlayerHistory(6, "RB", 4, targets=4, receptions=3, rec_yds=20, carries=24, rush_yds=100,
                    rush_tds=1, depth_rank=2)  # fmt: skip
QB1 = PlayerHistory(7, "QB", 4, carries=16, rush_yds=70, rush_tds=0, pass_attempts=132,
                    pass_yds=950, pass_tds=6, interceptions=3, depth_rank=1)  # fmt: skip
QB2 = PlayerHistory(8, "QB", 0, depth_rank=2)
ROOM = [WR1, WR2, WR3, TE1, RB1, RB2, QB1, QB2]
AVERAGE = TeamVolume(22.3, 33.0, 27.0, 31.0, 1.46, 0.92)


def out(player: PlayerHistory) -> PlayerHistory:
    return PlayerHistory(**{**player.__dict__, "available": False})


# Team volume -------------------------------------------------------------------------------------


def test_implied_points_split_the_total_by_the_spread() -> None:
    assert implied_points(47.0, 3.0) == 25.0
    assert implied_points(47.0, -3.0) == 22.0


def test_an_average_game_keeps_the_trailing_volume() -> None:
    volume = team_volume(TEAM, 2 * LEAGUE_IMPLIED_POINTS, 0.0)
    assert volume.pass_attempts == pytest.approx(33.0)
    assert volume.rush_attempts == pytest.approx(27.0)
    assert volume.targets == pytest.approx(31.0)
    assert volume.pass_tds + volume.rush_tds == pytest.approx(2.38, abs=0.01)


def test_a_bigger_implied_total_means_more_plays_and_touchdowns() -> None:
    low, high = team_volume(TEAM, 38.0, 0.0), team_volume(TEAM, 54.0, 0.0)
    assert high.pass_attempts + high.rush_attempts > low.pass_attempts + low.rush_attempts
    assert high.pass_tds + high.rush_tds > low.pass_tds + low.rush_tds


def test_favourites_run_more_than_underdogs_at_the_same_implied_total() -> None:
    # Same implied points (24), opposite sides of a 7-point spread.
    favourite, underdog = team_volume(TEAM, 41.0, 7.0), team_volume(TEAM, 55.0, -7.0)
    assert favourite.implied_points == underdog.implied_points
    assert pass_rate(favourite) < pass_rate(underdog)


def pass_rate(volume: TeamVolume) -> float:
    return volume.pass_attempts / (volume.pass_attempts + volume.rush_attempts)


def test_touchdowns_split_by_the_teams_pass_share_pulled_toward_the_league() -> None:
    passy = TeamHistory(4, 140, 100, 130, pass_tds=10, rush_tds=0, rec_tds=10)
    volume = team_volume(passy, 45.0, 0.0)
    share = volume.pass_tds / (volume.pass_tds + volume.rush_tds)
    assert 0.615 < share < 1.0
    assert share == pytest.approx((10 + 8 * 0.615) / 18)


def test_a_team_with_no_games_is_refused() -> None:
    with pytest.raises(ValueError, match="trailing game"):
        team_volume(TeamHistory(0, 0, 0, 0, 0, 0, 0), 45.0, 0.0)


# Shares -----------------------------------------------------------------------------------------


def test_shares_are_per_game_of_team_volume() -> None:
    found = shares(TEAM, ROOM)
    assert found[1].targets == pytest.approx(36 / 124)
    assert found[5].carries == pytest.approx(64 / 108)
    assert found[7].pass_attempts == pytest.approx(1.0)
    assert found[8] == Shares(0, 0, 0, 0, 0)


def test_a_player_back_from_injury_is_not_diluted_by_the_games_he_missed() -> None:
    returning = PlayerHistory(9, "WR", 2, targets=18)
    assert shares(TEAM, [returning])[9].targets == pytest.approx((18 / 2) / (124 / 4))


def test_a_wr1_ruled_out_hands_his_targets_to_teammates() -> None:
    before = shares(TEAM, ROOM)
    after = shares(TEAM, [out(WR1), *ROOM[1:]])

    assert after[1].targets == 0
    for player in (2, 3, 4, 5, 6):
        assert after[player].targets > before[player].targets
    assert sum(s.targets for s in after.values()) == pytest.approx(
        sum(s.targets for s in before.values())
    )
    assert sum(s.rec_tds for s in after.values()) == pytest.approx(
        sum(s.rec_tds for s in before.values())
    )


def test_a_promoted_backup_gets_a_starters_share() -> None:
    backup = PlayerHistory(2, "QB", 0, depth_rank=1)  # moved up to first on the chart
    found = shares(TEAM, [out(QB1), backup])
    assert found[2].pass_attempts >= PROMOTED_SHARE * 1.0 - 1e-9
    assert found[2].pass_attempts <= 1.0


def test_nobody_out_and_shares_under_100_percent_are_left_alone() -> None:
    found = shares(TEAM, [WR1, WR2])
    assert found[1].targets == pytest.approx(36 / 124)
    assert found[2].targets == pytest.approx(24 / 124)


players = st.builds(
    PlayerHistory,
    player_id=st.integers(1, 10_000),
    position=st.sampled_from(["QB", "RB", "WR", "TE"]),
    games=st.integers(0, 4),
    targets=st.floats(0, 60),
    carries=st.floats(0, 90),
    pass_attempts=st.floats(0, 160),
    rush_tds=st.floats(0, 6),
    rec_tds=st.floats(0, 6),
    available=st.booleans(),
    depth_rank=st.one_of(st.none(), st.integers(1, 4)),
)


@settings(max_examples=200, deadline=None)
@given(room=st.lists(players, max_size=12, unique_by=lambda p: p.player_id))
def test_shares_on_one_team_never_exceed_100_percent(room: list[PlayerHistory]) -> None:
    found = shares(TEAM, room)
    for kind in ("targets", "carries", "pass_attempts", "rush_tds", "rec_tds"):
        values = [getattr(s, kind) for s in found.values()]
        assert all(v >= 0 for v in values)
        assert sum(values) <= 1 + 1e-9
    for player in room:
        if not player.available:
            assert found[player.player_id] == Shares(0, 0, 0, 0, 0)


# Filling ---------------------------------------------------------------------------------------


def test_only_the_missing_stats_are_filled() -> None:
    share = shares(TEAM, ROOM)[5]
    assert fill(RB1, share, AVERAGE, set()) == {}
    assert set(fill(RB1, share, AVERAGE, {"rec_yds", "receptions"})) == {"rec_yds", "receptions"}
    # A stat the position isn't scored on is never filled.
    assert fill(WR1, shares(TEAM, ROOM)[1], AVERAGE, {"rush_yds", "pass_yds"}) == {}


def test_receiving_fill_is_targets_times_shrunk_efficiency() -> None:
    share = shares(TEAM, ROOM)[1]
    filled = fill(WR1, share, AVERAGE, {"rec_yds", "receptions", "anytime_td"})
    targets = 31.0 * 36 / 124
    assert filled["rec_yds"] == pytest.approx(targets * (320 + 30 * 7.92) / (36 + 30))
    assert filled["receptions"] == pytest.approx(targets * (24 + 30 * 0.631) / (36 + 30))
    assert filled["anytime_td"] == pytest.approx(1.46 * share.rec_tds)


def test_a_quarterbacks_filled_touchdowns_are_rushing_only() -> None:
    share = shares(TEAM, ROOM)[7]
    filled = fill(QB1, share, AVERAGE, {"anytime_td", "rush_yds", "pass_tds"})
    assert filled["anytime_td"] == pytest.approx(0.92 * share.rush_tds)
    assert filled["pass_tds"] == pytest.approx(1.46)
    assert filled["rush_yds"] > 0


def test_a_player_with_no_share_fills_zeros() -> None:
    filled = fill(QB2, shares(TEAM, ROOM)[8], AVERAGE, {"pass_yds", "rush_yds"})
    assert filled == {"pass_yds": 0.0, "rush_yds": 0.0}


@pytest.mark.parametrize(
    "model",
    [
        StatModel("gamma", 3.0, ZeroInflation((10.0, 40.0), (0.15, 0.02))),
        StatModel("lognormal", 0.6),
        StatModel("negbin", 16.0),
        StatModel("poisson"),
        StatModel("bernoulli"),
    ],
)
@pytest.mark.parametrize("mean", [0.0, 0.4, 12.0, 61.0])
def test_a_filled_distribution_keeps_the_mean(model: StatModel, mean: float) -> None:
    fitted = distribution(model, mean)
    assert fitted.mean == pytest.approx(mean, abs=1e-9)
    assert fitted.quantile(0.2) <= fitted.quantile(0.8)


# In the projection -------------------------------------------------------------------------------


POISSON = StatModel("poisson")


def model_input(mean: float, model: StatModel = POISSON) -> StatInput:
    return StatInput(distribution(model, mean), books=0, source="model")


def test_a_fully_modelled_player_is_scored_and_marked_low_confidence() -> None:
    gamma = StatModel("gamma", 2.5)
    result = project(
        "WR",
        PPR,
        {
            "rec_yds": model_input(30.0, gamma),
            "receptions": model_input(2.5),
            "anytime_td": model_input(0.2),
        },
        0.02,
    )
    assert result.mean == pytest.approx(0.1 * 30 + 2.5 + 6 * 0.2 - 0.04)
    assert {b.source for s, b in result.breakdown.items() if s != "fumbles_lost"} == {"model"}
    assert all(b.books is None for s, b in result.breakdown.items() if s != "fumbles_lost")
    assert result.confidence.score == pytest.approx(MODEL_CREDIT)
    assert result.confidence.modelled == ("anytime_td", "rec_yds", "receptions")
    assert result.confidence.missing == ()


def test_a_fill_scores_less_confidence_than_a_market_and_more_than_nothing() -> None:
    from engine.distributions import fit_anytime_td

    market_td = StatInput(fit_anytime_td(0.3), books=4, dispersion=0.0)
    base = {"anytime_td": market_td}
    yards = model_input(40.0, StatModel("gamma", 2.5))
    with_fill = project("WR", PPR, {**base, "rec_yds": yards}, 0.0).confidence
    without = project("WR", PPR, base, 0.0).confidence
    assert with_fill.score > without.score
    assert with_fill.score < 1.0
    # Book quality is judged on the market stats only.
    assert (with_fill.books, with_fill.dispersion, with_fill.freshness) == (1.0, 1.0, 1.0)
