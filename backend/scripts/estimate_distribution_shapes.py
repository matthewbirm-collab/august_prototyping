"""Estimate each stat's distribution shape from nflverse game logs.

    cd backend && uv run python scripts/estimate_distribution_shapes.py

Writes backend/config/distribution_params.json (read by the consensus job and, later, the
projection) and docs/DISTRIBUTION_SHAPES.md (the evidence behind it). Downloads public nflverse
data (CC-BY 4.0) through nflreadpy; needs no API key.
"""

import argparse
import json
from datetime import date
from pathlib import Path

import nflreadpy as nfl
import polars as pl
from nflreadpy.config import update_config

from analytics.shapes import build_params, estimate_all, render_report

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--first-season", type=int, default=2021)
    parser.add_argument("--last-season", type=int, default=2025)
    parser.add_argument(
        "--config", type=Path, default=BACKEND / "config" / "distribution_params.json"
    )
    parser.add_argument("--report", type=Path, default=REPO / "docs" / "DISTRIBUTION_SHAPES.md")
    args = parser.parse_args()

    update_config(cache_mode="filesystem", cache_dir=BACKEND / ".cache" / "nflverse")
    seasons = list(range(args.first_season, args.last_season + 1))
    games = nfl.load_player_stats(seasons).filter(pl.col("season_type") == "REG")
    print(f"{games.height:,} player-games, seasons {seasons[0]}-{seasons[-1]}")

    estimates = estimate_all(games)
    span = f"{seasons[0]}-{seasons[-1]}"
    source = "nflverse weekly player stats (CC-BY 4.0)"
    today = date.today().isoformat()

    args.config.parent.mkdir(parents=True, exist_ok=True)
    params = build_params(estimates, source=f"{source}, regular seasons {span}", generated=today)
    args.config.write_text(json.dumps(params, indent=2) + "\n")
    args.report.write_text(render_report(estimates, source=source, generated=today, seasons=span))
    print(f"wrote {args.config.relative_to(REPO)} and {args.report.relative_to(REPO)}")


if __name__ == "__main__":
    main()
