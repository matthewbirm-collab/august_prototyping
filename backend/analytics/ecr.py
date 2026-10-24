"""Turn FantasyPros expert-consensus ranks (ECR) into points, so they can be graded like ours.

ECR is a rank within a position each week. The baseline projection for "the RB ranked 12th" is
what RBs ranked 12th actually scored on average in earlier seasons: a curve per position and
scoring config, smoothed to be non-increasing in rank. A season is only ever graded with a curve
fitted on the seasons before it, so the baseline can't see its own outcomes.

The curves live in `config/ecr_points_curve.json`, written by `scripts/build_ecr_curve.py`.
"""

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from analytics.shapes import non_increasing

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "ecr_points_curve.json"
MAX_RANK = {"QB": 40, "RB": 80, "WR": 100, "TE": 40}


def position_ranks(ecr: Mapping[int, float]) -> dict[int, int]:
    """player -> 1-based rank by ECR (lower is better) within one week's position list."""
    ordered = sorted(ecr.items(), key=lambda item: (item[1], item[0]))
    return {player: i + 1 for i, (player, _) in enumerate(ordered)}


def fit_curve(pairs: Iterable[tuple[int, float]], max_rank: int) -> list[float]:
    """Mean points at each rank 1..max_rank, made non-increasing. Ranks past `max_rank` pool
    into the last one; a rank never seen takes the value of the rank above it."""
    sums: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for rank, points in pairs:
        entry = sums[min(rank, max_rank)]
        entry[0] += points
        entry[1] += 1
    values: list[float] = []
    counts: list[int] = []
    for rank in range(1, max_rank + 1):
        total, n = sums.get(rank, [0.0, 0.0])
        if n:
            values.append(total / n)
            counts.append(int(n))
        else:
            values.append(values[-1] if values else 0.0)
            counts.append(0)
    if not any(counts):
        return values
    # Give empty ranks a token weight so pooling still covers them.
    return non_increasing(values, [max(c, 1) for c in counts])


@dataclass(frozen=True)
class EcrCurves:
    """curves[graded season][scoring key][position] -> points at rank 1, 2, ..."""

    curves: Mapping[str, Mapping[str, Mapping[str, Sequence[float]]]]

    def points(self, season: int, scoring: str, position: str, rank: int) -> float | None:
        curve = self.curves.get(str(season), {}).get(scoring, {}).get(position)
        if not curve or rank < 1:
            return None
        return float(curve[min(rank, len(curve)) - 1])

    def seasons(self) -> set[int]:
        return {int(s) for s in self.curves}


def load_curves(path: Path = DEFAULT_PATH) -> EcrCurves:
    if not path.exists():
        return EcrCurves({})
    data = json.loads(path.read_text())
    return EcrCurves(data["curves"])
