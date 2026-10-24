import pytest

from ingest.names import normalize_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("D.J. Moore", "dj moore"),
        ("DJ Moore", "dj moore"),
        ("D J Moore", "dj moore"),
        ("A.J. Brown", "aj brown"),
        ("Marvin Harrison Jr.", "marvin harrison"),
        ("Kenneth Walker III", "kenneth walker"),
        ("Odell Beckham Jr. II", "odell beckham"),
        ("Ja'Marr Chase", "jamarr chase"),
        ("Ja" + chr(0x2019) + "Marr Chase", "jamarr chase"),  # curly apostrophe
        ("Amon-Ra St. Brown", "amon ra st brown"),
        ("José Ñúñez", "jose nunez"),
        ("  Travis   KELCE ", "travis kelce"),
        ("Josh V", "josh v"),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected
