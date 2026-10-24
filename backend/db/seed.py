"""Load reference data (teams, books, markets, built-in scoring configs). Safe to rerun.

Usage: `make seed`, or `uv run python -m db.seed` from backend/.
"""

import logging

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.logging import configure_logging
from app.settings import get_settings
from db.base import Base
from db.models import Book, Market, ScoringConfig, Team
from db.seed_data import BOOKS, MARKETS, SCORING_CONFIGS, TEAMS
from db.session import get_engine

logger = logging.getLogger(__name__)


def _upsert(session: Session, model: type[Base], rows: list[dict[str, object]], key: str) -> None:
    """Insert rows, updating the listed columns of rows that already exist."""
    stmt = insert(model).values(rows)
    updates = {col: stmt.excluded[col] for col in rows[0] if col != key}
    session.execute(stmt.on_conflict_do_update(index_elements=[key], set_=updates))


def seed(session: Session) -> None:
    _upsert(session, Team, [dict(t) for t in TEAMS], "abbr")
    # sharpness_weight and active are left alone on reruns, so tuning them by hand sticks.
    _upsert(session, Book, [dict(b) for b in BOOKS], "key")
    # Base markets first: alternates reference them.
    ordered = sorted(MARKETS, key=lambda m: m["is_alternate"])
    _upsert(session, Market, [dict(m) for m in ordered], "key")
    _upsert(
        session,
        ScoringConfig,
        [
            {
                "key": c["key"],
                "name": c["name"],
                "weights": c["weights"].model_dump(),
                "is_builtin": True,
            }
            for c in SCORING_CONFIGS
        ],
        "key",
    )


def main() -> None:
    configure_logging(get_settings().log_level)
    with Session(get_engine()) as session, session.begin():
        seed(session)
    logger.info(
        "seed loaded",
        extra={
            "teams": len(TEAMS),
            "books": len(BOOKS),
            "markets": len(MARKETS),
            "scoring_configs": len(SCORING_CONFIGS),
        },
    )


if __name__ == "__main__":
    main()
