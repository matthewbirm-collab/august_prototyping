"""Fantasy-relevant prop market keys, taken from the seeded `markets` table."""

from db.seed_data import MARKETS

MAIN_MARKETS: tuple[str, ...] = tuple(m["key"] for m in MARKETS if not m["is_alternate"])
ALTERNATE_MARKETS: tuple[str, ...] = tuple(m["key"] for m in MARKETS if m["is_alternate"])
FANTASY_MARKETS: tuple[str, ...] = MAIN_MARKETS + ALTERNATE_MARKETS
