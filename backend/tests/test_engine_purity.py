"""engine/ is a pure library (CLAUDE.md): the same code runs live and in backtests, so it must
not reach into the database, the HTTP clients or the rest of the application."""

import ast
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "engine"
FORBIDDEN = {
    # Our own layers.
    "app",
    "db",
    "ingest",
    "analytics",
    # Databases, caches and web frameworks.
    "sqlalchemy",
    "alembic",
    "psycopg",
    "redis",
    "fastapi",
    # Network access and data sources.
    "httpx",
    "requests",
    "urllib",
    "http",
    "socket",
    "nflreadpy",
}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


ENGINE_FILES = sorted(ENGINE.rglob("*.py"))


def test_there_are_engine_modules_to_check() -> None:
    assert {p.name for p in ENGINE_FILES} >= {"odds.py", "scoring.py"}


@pytest.mark.parametrize("path", ENGINE_FILES, ids=lambda p: p.name)
def test_engine_modules_import_nothing_from_the_outside_world(path: Path) -> None:
    assert imported_modules(path) & FORBIDDEN == set()


def test_the_check_would_catch_a_violation(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import sqlalchemy\nfrom db.models import Game\nfrom urllib.request import urlopen\n"
    )
    assert imported_modules(bad) & FORBIDDEN == {"sqlalchemy", "db", "urllib"}
