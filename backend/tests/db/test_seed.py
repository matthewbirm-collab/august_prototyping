from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from db.models import Book, Market, ScoringConfig, Team
from db.seed import seed
from db.seed_data import BOOKS, MARKETS, SCORING_CONFIGS, TEAMS
from engine.scoring import ScoringWeights


def _counts(session: Session) -> tuple[int, ...]:
    return tuple(
        session.scalar(select(func.count()).select_from(model)) or 0
        for model in (Team, Book, Market, ScoringConfig)
    )


def test_seed_loads_reference_data(session: Session) -> None:
    seed(session)
    session.commit()
    assert _counts(session) == (len(TEAMS), len(BOOKS), len(MARKETS), len(SCORING_CONFIGS))
    assert {"ppr", "half_ppr", "standard"} == set(session.scalars(select(ScoringConfig.key)))


def test_seed_is_idempotent(session: Session) -> None:
    seed(session)
    session.commit()
    before = _counts(session)
    seed(session)
    session.commit()
    assert _counts(session) == before


def test_reseed_keeps_hand_tuned_book_weights(session: Session) -> None:
    seed(session)
    session.execute(update(Book).where(Book.key == "fanduel").values(sharpness_weight=1.5))
    session.commit()
    seed(session)
    session.commit()
    assert session.scalar(select(Book.sharpness_weight).where(Book.key == "fanduel")) == 1.5


def test_seeded_scoring_configs_are_valid(session: Session) -> None:
    seed(session)
    session.commit()
    configs = {
        c.key: ScoringWeights.model_validate(c.weights)
        for c in session.scalars(select(ScoringConfig))
    }
    assert configs["ppr"].reception == 1
    assert configs["half_ppr"].reception == 0.5
    assert configs["standard"].reception == 0
    assert configs["ppr"].pass_td == 4


def test_alternate_markets_point_at_their_base(session: Session) -> None:
    seed(session)
    session.commit()
    alternates = session.scalars(select(Market).where(Market.is_alternate)).all()
    assert alternates
    for market in alternates:
        assert market.base_market_key == market.key.removesuffix("_alternate")
