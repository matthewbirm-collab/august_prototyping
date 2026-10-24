"""What the live capture tells the code that derives data from new snapshots (consensus now,
projections later): a game, the players whose quotes changed, and the moment of the capture."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecomputeRequest:
    game_id: str
    player_ids: tuple[int, ...]
    """Players with at least one new snapshot row, sorted."""
    as_of: datetime
    """When the provider says the snapshot was taken: the moment to compute as of."""


RecomputeHook = Callable[[RecomputeRequest], None]


def log_recompute(request: RecomputeRequest) -> None:
    """The default hook: note that a recompute was requested and do nothing."""
    logger.info(
        "recompute requested",
        extra={"game": request.game_id, "players": len(request.player_ids)},
    )
