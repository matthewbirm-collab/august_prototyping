from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Game, Player, PlayerAlias, Roster, UnmatchedName
from db.seed import seed
from db.snapshots import SnapshotQuote, insert_prop_snapshots
from ingest import resolver_cli
from ingest.resolver import (
    GameContext,
    PlayerResolver,
    position_hint_for_market,
    weekly_unmatched_rate,
)
from ingest.rosters import DbRosterLookup

SOURCE = "the_odds_api"
KICKOFF = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)

# (player_id, name, position, {(season, week): team})
PLAYERS = [
    (1, "Patrick Mahomes", "QB", {(2026, 8): "KC"}),
    (2, "Isiah Pacheco", "RB", {(2026, 8): "KC"}),
    (3, "Travis Kelce", "TE", {(2026, 8): "KC"}),
    (4, "Mike Williams", "WR", {(2026, 8): "KC"}),
    (5, "Kyle Williams", "WR", {(2026, 8): "KC"}),
    (6, "Marvin Harrison Jr.", "WR", {(2026, 8): "LV"}),
    (7, "DJ Moore", "WR", {(2026, 8): "LV"}),
    (8, "Ja'Marr Chase", "WR", {(2026, 8): "LV"}),
    (9, "Gabriel Davis", "WR", {(2026, 8): "LV"}),
    (10, "Josh Allen", "QB", {(2026, 8): "BUF"}),
    (11, "Tyreek Hill", "WR", {(2026, 8): "MIA"}),
    (12, "Josh Allen", "DE", {(2026, 8): "JAX"}),
    (13, "Travis Etienne", "RB", {(2026, 8): "JAX"}),
    (14, "Mike Williams", "WR", {(2026, 8): "NYJ"}),
    (15, "Mike Williams", "WR", {(2026, 8): "LAC"}),
    (16, "Amari Cooper", "WR", {(2026, 5): "CLE", (2026, 8): "BUF"}),
    (17, "Jerry Jeudy", "WR", {(2026, 5): "CLE", (2026, 8): "CLE"}),
    (20, "Chris Jones", "RB", {(2026, 8): "NYJ"}),
    (21, "Chris Jones", "TE", {(2026, 8): "LAC"}),
]

GAMES = {
    "kc_lv": ("2026_08_KC_LV", 8, "LV", "KC"),
    "buf_mia": ("2026_08_BUF_MIA", 8, "MIA", "BUF"),
    "jax_ten": ("2026_08_JAX_TEN", 8, "TEN", "JAX"),
    "nyj_lac": ("2026_08_NYJ_LAC", 8, "LAC", "NYJ"),
    "cle_pit_wk5": ("2026_05_CLE_PIT", 5, "PIT", "CLE"),
    "cle_cin": ("2026_08_CLE_CIN", 8, "CIN", "CLE"),
}


@pytest.fixture
def db(session: Session) -> Iterator[Session]:
    seed(session)
    for game_id, week, home, away in GAMES.values():
        session.add(
            Game(
                id=game_id,
                season=2026,
                week=week,
                season_type="REG",
                kickoff_utc=KICKOFF - timedelta(weeks=8 - week),
                home_abbr=home,
                away_abbr=away,
            )
        )
    for player_id, name, position, teams in PLAYERS:
        session.add(Player(id=player_id, full_name=name, position=position))
        session.flush()
        for (season, week), team in teams.items():
            session.add(
                Roster(
                    season=season,
                    week=week,
                    player_id=player_id,
                    team_abbr=team,
                    position=position,
                    available_at=KICKOFF - timedelta(weeks=8 - week, days=3),
                )
            )
    session.commit()
    yield session


def game(key: str) -> GameContext:
    game_id, week, home, away = GAMES[key]
    return GameContext(game_id, 2026, week, home, away)


def resolver(db: Session) -> PlayerResolver:
    return PlayerResolver(db, DbRosterLookup(db), source=SOURCE, fuzzy_threshold=90)


def resolve(db: Session, key: str, raw: str, **kwargs: object) -> tuple[int | None, str]:
    result = resolver(db).for_game(game(key)).resolve(raw, **kwargs)  # type: ignore[arg-type]
    db.commit()
    return result.player_id, result.method


def open_unmatched(db: Session) -> list[str]:
    return list(db.scalars(select(UnmatchedName.raw_name).where(UnmatchedName.status == "open")))


# Matching --------------------------------------------------------------------------------------


def test_exact_normalized_name(db: Session) -> None:
    assert resolve(db, "kc_lv", "Patrick Mahomes") == (1, "normalized")


def test_match_is_saved_as_an_alias_and_reused(db: Session) -> None:
    resolve(db, "kc_lv", "Travis Kelce")
    alias = db.scalars(select(PlayerAlias).where(PlayerAlias.raw_name == "Travis Kelce")).one()
    assert (alias.player_id, alias.method, alias.score) == (3, "normalized", 100.0)
    assert resolve(db, "kc_lv", "Travis Kelce") == (3, "alias")


def test_suffix_is_ignored_both_ways(db: Session) -> None:
    assert resolve(db, "kc_lv", "Marvin Harrison") == (6, "normalized")


def test_initials(db: Session) -> None:
    assert resolve(db, "kc_lv", "D.J. Moore") == (7, "normalized")


def test_apostrophes_and_accents(db: Session) -> None:
    assert resolve(db, "kc_lv", "JaMarr Chase") == (8, "normalized")


def test_small_typo_matches_fuzzily(db: Session) -> None:
    player_id, method = resolve(db, "kc_lv", "Travis Kelcee")
    assert (player_id, method) == (3, "fuzzy")


def test_weak_fuzzy_match_goes_unmatched(db: Session) -> None:
    assert resolve(db, "kc_lv", "T. Kelce") == (None, "unmatched")
    assert open_unmatched(db) == ["T. Kelce"]


def test_shared_last_name_on_one_team_never_guesses(db: Session) -> None:
    assert resolve(db, "kc_lv", "M. Williams") == (None, "unmatched")
    assert resolve(db, "kc_lv", "Kyle Williams") == (5, "normalized")
    assert resolve(db, "kc_lv", "Mike Williams") == (4, "normalized")


def test_same_name_elsewhere_in_league_uses_this_games_rosters(db: Session) -> None:
    assert resolve(db, "buf_mia", "Josh Allen") == (10, "normalized")
    # The saved alias points at the Bills QB, who isn't in this game, so name matching runs.
    assert resolve(db, "jax_ten", "Josh Allen") == (12, "normalized")
    assert set(db.scalars(select(PlayerAlias.player_id))) == {10, 12}


def test_same_name_on_both_teams_goes_unmatched(db: Session) -> None:
    assert resolve(db, "nyj_lac", "Mike Williams") == (None, "unmatched")


def test_team_hint_resolves_a_same_name_tie(db: Session) -> None:
    assert resolve(db, "nyj_lac", "Mike Williams", team_hint="LAC") == (15, "normalized")


def test_position_hint_breaks_a_tie(db: Session) -> None:
    rushing = position_hint_for_market("player_rush_yds")
    assert resolve(db, "nyj_lac", "Chris Jones", position_hint=rushing) == (20, "normalized")


def test_position_hint_that_fits_both_still_goes_unmatched(db: Session) -> None:
    receiving = position_hint_for_market("player_reception_yds_alternate")
    assert resolve(db, "nyj_lac", "Chris Jones", position_hint=receiving) == (None, "unmatched")


def test_traded_player_alias_follows_him_to_his_new_team(db: Session) -> None:
    assert resolve(db, "cle_pit_wk5", "Amari Cooper") == (16, "normalized")
    assert resolve(db, "buf_mia", "Amari Cooper") == (16, "alias")


def test_traded_player_alias_does_not_match_in_old_teams_games(db: Session) -> None:
    resolve(db, "cle_pit_wk5", "Amari Cooper")
    assert resolve(db, "cle_cin", "Amari Cooper") == (None, "unmatched")


def test_name_change_needs_a_manual_confirm_then_sticks(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    assert resolve(db, "kc_lv", "Gabe Davis") == (None, "unmatched")
    unmatched_id = db.scalars(select(UnmatchedName.id)).one()

    assert resolver_cli.main(["confirm", str(unmatched_id), "9"], session=db) == 0
    assert "Gabriel Davis" in capsys.readouterr().out
    assert resolve(db, "kc_lv", "Gabe Davis") == (9, "alias")
    assert open_unmatched(db) == []


def test_repeat_sightings_increment_occurrences(db: Session) -> None:
    resolve(db, "kc_lv", "Nobody Here")
    resolve(db, "kc_lv", "Nobody Here")
    row = db.scalars(select(UnmatchedName)).one()
    assert row.occurrences == 2
    assert row.game_id == "2026_08_KC_LV"


def test_no_rosters_means_unmatched(db: Session) -> None:
    empty = GameContext("2026_08_KC_LV", 2026, 9, "LV", "KC")
    result = resolver(db).for_game(empty).resolve("Patrick Mahomes")
    assert (result.player_id, result.method) == (None, "unmatched")


# Rate and CLI ----------------------------------------------------------------------------------


def _snapshot(player_id: int) -> SnapshotQuote:
    return SnapshotQuote(
        captured_at=KICKOFF - timedelta(hours=6),
        game_id="2026_08_KC_LV",
        book_key="draftkings",
        market_key="player_rush_yds",
        player_id=player_id,
        line=50.5,
        over_price=-110,
        under_price=-110,
        source="live",
    )


def test_weekly_unmatched_rate(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    insert_prop_snapshots(db, [_snapshot(1), _snapshot(2)])
    resolve(db, "kc_lv", "Nobody Here")

    result = weekly_unmatched_rate(db, 2026, 8)
    assert (result.unmatched, result.matched_players) == (1, 2)
    assert result.rate == pytest.approx(1 / 3)

    assert resolver_cli.main(["rate", "--season", "2026", "--week", "8"], session=db) == 0
    assert "33.3%" in capsys.readouterr().out
    assert weekly_unmatched_rate(db, 2026, 7).rate == 0.0


def test_cli_list_shows_suggestions(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    resolve(db, "kc_lv", "T. Kelce")
    assert resolver_cli.main(["list", "--season", "2026", "--week", "8"], session=db) == 0
    out = capsys.readouterr().out
    assert "'T. Kelce'" in out
    assert "3: Travis Kelce (TE, KC)" in out


def test_cli_ignore_hides_a_name(db: Session) -> None:
    resolve(db, "kc_lv", "Nobody Here")
    unmatched_id = db.scalars(select(UnmatchedName.id)).one()
    assert resolver_cli.main(["ignore", str(unmatched_id)], session=db) == 0
    assert open_unmatched(db) == []


def test_cli_confirm_rejects_unknown_ids(db: Session) -> None:
    assert resolver_cli.main(["confirm", "999", "1"], session=db) == 1
