"""Player-name normalization shared by the resolver and its admin CLI. Pure; no I/O."""

import re
import unicodedata

SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})
_APOSTROPHES = re.compile("['\u2018\u2019`]")  # straight and curly apostrophes, backtick
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str) -> str:
    """Lowercase, strip accents, apostrophes, punctuation and generational suffixes, and join
    runs of initials, so "D.J. Moore", "DJ Moore" and "D J Moore" all become "dj moore".
    """
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()
    tokens = _NON_ALNUM.sub(" ", _APOSTROPHES.sub("", ascii_name)).split()

    # A suffix only counts after a first and last name, so "Kenneth Walker III" loses "iii"
    # but a two-token name like "Josh V" is left alone.
    while len(tokens) > 2 and tokens[-1] in SUFFIXES:
        tokens.pop()

    joined: list[str] = []
    run: list[str] = []
    for token in [*tokens, ""]:
        if len(token) == 1:
            run.append(token)
            continue
        if run:
            joined.append("".join(run))
            run = []
        if token:
            joined.append(token)
    return " ".join(joined)
