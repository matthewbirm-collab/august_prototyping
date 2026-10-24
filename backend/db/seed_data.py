"""Reference data loaded by `make seed`.

Books and market keys are provisional: `/p2-odds-adapter` confirms them against the provider's
lists. Every book starts at the same sharpness weight until the "which books are sharp" decision
in docs/ROADMAP.md is made.
"""

from typing import TypedDict

from engine.scoring import ScoringWeights, standard_weights


class TeamRow(TypedDict):
    abbr: str
    name: str
    conference: str
    division: str


class BookRow(TypedDict):
    key: str
    display_name: str


class MarketRow(TypedDict):
    key: str
    stat: str
    distribution_family: str
    is_alternate: bool
    base_market_key: str | None


class ScoringRow(TypedDict):
    key: str
    name: str
    weights: ScoringWeights


def _division(conference: str, division: str, teams: list[tuple[str, str]]) -> list[TeamRow]:
    return [
        TeamRow(abbr=abbr, name=name, conference=conference, division=f"{conference} {division}")
        for abbr, name in teams
    ]


# nflverse abbreviations.
TEAMS: list[TeamRow] = [
    *_division(
        "AFC",
        "East",
        [
            ("BUF", "Buffalo Bills"),
            ("MIA", "Miami Dolphins"),
            ("NE", "New England Patriots"),
            ("NYJ", "New York Jets"),
        ],
    ),
    *_division(
        "AFC",
        "North",
        [
            ("BAL", "Baltimore Ravens"),
            ("CIN", "Cincinnati Bengals"),
            ("CLE", "Cleveland Browns"),
            ("PIT", "Pittsburgh Steelers"),
        ],
    ),
    *_division(
        "AFC",
        "South",
        [
            ("HOU", "Houston Texans"),
            ("IND", "Indianapolis Colts"),
            ("JAX", "Jacksonville Jaguars"),
            ("TEN", "Tennessee Titans"),
        ],
    ),
    *_division(
        "AFC",
        "West",
        [
            ("DEN", "Denver Broncos"),
            ("KC", "Kansas City Chiefs"),
            ("LV", "Las Vegas Raiders"),
            ("LAC", "Los Angeles Chargers"),
        ],
    ),
    *_division(
        "NFC",
        "East",
        [
            ("DAL", "Dallas Cowboys"),
            ("NYG", "New York Giants"),
            ("PHI", "Philadelphia Eagles"),
            ("WAS", "Washington Commanders"),
        ],
    ),
    *_division(
        "NFC",
        "North",
        [
            ("CHI", "Chicago Bears"),
            ("DET", "Detroit Lions"),
            ("GB", "Green Bay Packers"),
            ("MIN", "Minnesota Vikings"),
        ],
    ),
    *_division(
        "NFC",
        "South",
        [
            ("ATL", "Atlanta Falcons"),
            ("CAR", "Carolina Panthers"),
            ("NO", "New Orleans Saints"),
            ("TB", "Tampa Bay Buccaneers"),
        ],
    ),
    *_division(
        "NFC",
        "West",
        [
            ("ARI", "Arizona Cardinals"),
            ("LA", "Los Angeles Rams"),
            ("SF", "San Francisco 49ers"),
            ("SEA", "Seattle Seahawks"),
        ],
    ),
]

# The Odds API `us` region bookmaker keys.
BOOKS: list[BookRow] = [
    BookRow(key="draftkings", display_name="DraftKings"),
    BookRow(key="fanduel", display_name="FanDuel"),
    BookRow(key="betmgm", display_name="BetMGM"),
    BookRow(key="williamhill_us", display_name="Caesars"),
    BookRow(key="espnbet", display_name="ESPN BET"),
    BookRow(key="fanatics", display_name="Fanatics"),
    BookRow(key="betrivers", display_name="BetRivers"),
    BookRow(key="betonlineag", display_name="BetOnline.ag"),
    BookRow(key="bovada", display_name="Bovada"),
    BookRow(key="lowvig", display_name="LowVig.ag"),
    BookRow(key="mybookieag", display_name="MyBookie.ag"),
    BookRow(key="betus", display_name="BetUS"),
]


def _market(key: str, stat: str, family: str) -> MarketRow:
    return MarketRow(
        key=key, stat=stat, distribution_family=family, is_alternate=False, base_market_key=None
    )


def _alternate(base: MarketRow) -> MarketRow:
    return MarketRow(
        key=f"{base['key']}_alternate",
        stat=base["stat"],
        distribution_family=base["distribution_family"],
        is_alternate=True,
        base_market_key=base["key"],
    )


_PASS_YDS = _market("player_pass_yds", "pass_yds", "gamma")
_PASS_TDS = _market("player_pass_tds", "pass_tds", "poisson")
_RUSH_YDS = _market("player_rush_yds", "rush_yds", "gamma")
_RECEPTIONS = _market("player_receptions", "receptions", "poisson")
_REC_YDS = _market("player_reception_yds", "rec_yds", "gamma")

MARKETS: list[MarketRow] = [
    _PASS_YDS,
    _PASS_TDS,
    _market("player_pass_completions", "pass_completions", "negbin"),
    _market("player_pass_interceptions", "interceptions", "poisson"),
    _RUSH_YDS,
    _market("player_rush_attempts", "rush_attempts", "negbin"),
    _RECEPTIONS,
    _REC_YDS,
    _market("player_anytime_td", "anytime_td", "bernoulli"),
    *(_alternate(m) for m in (_PASS_YDS, _PASS_TDS, _RUSH_YDS, _RECEPTIONS, _REC_YDS)),
]

SCORING_CONFIGS: list[ScoringRow] = [
    ScoringRow(key="ppr", name="PPR", weights=standard_weights(reception=1)),
    ScoringRow(key="half_ppr", name="Half-PPR", weights=standard_weights(reception=0.5)),
    ScoringRow(key="standard", name="Standard", weights=standard_weights(reception=0)),
]
