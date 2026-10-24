"""Grade replayed projections against what happened, and compare them with the ECR baseline.

Per snapshot label (t24h, t1h, close) and position:

    MAE, RMSE, bias    projected minus actual fantasy points
    Spearman           rank correlation within each week's position group, averaged over weeks
    coverage           share of outcomes below p20 (target 20%) and below p80 (target 80%)
    vs ECR             MAE of ours and of the ECR baseline on the players both project

The Phase 3 gate is that we beat ECR on MAE at every position. A run also fails when any
position's MAE is more than `REGRESSION_TOLERANCE` worse than the saved baseline.
"""

import csv
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from scipy import stats

POSITIONS = ("QB", "RB", "WR", "TE")
REGRESSION_TOLERANCE = 0.05
MIN_RANK_GROUP = 5
"""A week's position group needs this many players for its rank correlation to count."""


@dataclass(frozen=True)
class Graded:
    season: int
    week: int
    game_id: str
    label: str
    player_id: int
    position: str
    projected: float
    p20: float
    p80: float
    actual: float
    ecr: float | None = None
    """The ECR baseline's points for this player and week, if ranked."""
    model_share: float = 0.0
    """Share of the projected points (by absolute size) that came from gap-filled stats."""
    confidence: float = 1.0


@dataclass(frozen=True)
class PositionReport:
    label: str
    position: str
    n: int
    mae: float
    rmse: float
    bias: float
    spearman: float | None
    below_p20: float
    below_p80: float
    model_share: float
    head_to_head: int
    """Players both we and ECR projected."""
    mae_ours: float | None
    mae_ecr: float | None

    @property
    def beats_ecr(self) -> bool | None:
        if self.mae_ours is None or self.mae_ecr is None:
            return None
        return self.mae_ours < self.mae_ecr


def evaluate(rows: Iterable[Graded]) -> dict[tuple[str, str], PositionReport]:
    """One report per (label, position) that has graded rows, in label then position order."""
    groups: dict[tuple[str, str], list[Graded]] = defaultdict(list)
    for row in rows:
        groups[(row.label, row.position)].append(row)
    order = {p: i for i, p in enumerate(POSITIONS)}
    reports: dict[tuple[str, str], PositionReport] = {}
    for key in sorted(groups, key=lambda k: (k[0], order.get(k[1], 99), k[1])):
        reports[key] = _report(key[0], key[1], groups[key])
    return reports


def _report(label: str, position: str, rows: Sequence[Graded]) -> PositionReport:
    errors = [r.projected - r.actual for r in rows]
    n = len(rows)
    both = [r for r in rows if r.ecr is not None]
    return PositionReport(
        label=label,
        position=position,
        n=n,
        mae=sum(abs(e) for e in errors) / n,
        rmse=math.sqrt(sum(e * e for e in errors) / n),
        bias=sum(errors) / n,
        spearman=weekly_spearman(rows),
        below_p20=sum(1 for r in rows if r.actual < r.p20) / n,
        below_p80=sum(1 for r in rows if r.actual < r.p80) / n,
        model_share=sum(r.model_share for r in rows) / n,
        head_to_head=len(both),
        mae_ours=_mae((r.projected, r.actual) for r in both) if both else None,
        mae_ecr=_mae((r.ecr or 0.0, r.actual) for r in both) if both else None,
    )


def _mae(pairs: Iterable[tuple[float, float]]) -> float:
    found = [abs(p - a) for p, a in pairs]
    return sum(found) / len(found)


def weekly_spearman(rows: Sequence[Graded]) -> float | None:
    """Mean over weeks of the rank correlation between projected and actual points."""
    weeks: dict[tuple[int, int], list[Graded]] = defaultdict(list)
    for row in rows:
        weeks[(row.season, row.week)].append(row)
    found = []
    for group in weeks.values():
        if len(group) < MIN_RANK_GROUP:
            continue
        projected, actual = [r.projected for r in group], [r.actual for r in group]
        if len(set(projected)) < 2 or len(set(actual)) < 2:
            continue
        found.append(float(stats.spearmanr(projected, actual).statistic))
    return sum(found) / len(found) if found else None


# Gate and regression ----------------------------------------------------------------------------


def gate(reports: Mapping[tuple[str, str], PositionReport], label: str) -> dict[str, bool | None]:
    """position -> whether we beat ECR on MAE at this label (None: no head-to-head)."""
    return {p: r.beats_ecr for (lab, p), r in reports.items() if lab == label}


def baseline_from(reports: Mapping[tuple[str, str], PositionReport]) -> dict[str, dict[str, float]]:
    """label -> position -> MAE, the shape stored in `baseline.json`."""
    found: dict[str, dict[str, float]] = defaultdict(dict)
    for (label, position), report in reports.items():
        found[label][position] = round(report.mae, 4)
    return dict(found)


def regressions(
    reports: Mapping[tuple[str, str], PositionReport],
    baseline: Mapping[str, Mapping[str, float]],
    tolerance: float = REGRESSION_TOLERANCE,
) -> list[str]:
    """Positions whose MAE is more than `tolerance` worse than the baseline, described."""
    found = []
    for (label, position), report in reports.items():
        before = baseline.get(label, {}).get(position)
        if before is not None and report.mae > before * (1 + tolerance):
            found.append(
                f"{label} {position}: MAE {report.mae:.3f} vs baseline {before:.3f} "
                f"(+{report.mae / before - 1:.1%})"
            )
    return found


def load_baseline(path: Path, key: str) -> dict[str, dict[str, float]]:
    """The saved MAEs for this run kind (`key`, e.g. "market/ppr"), or empty."""
    if not path.exists():
        return {}
    stored: dict[str, dict[str, dict[str, float]]] = json.loads(path.read_text())
    return stored.get(key, {})


def save_baseline(path: Path, key: str, values: Mapping[str, Mapping[str, float]]) -> None:
    stored = json.loads(path.read_text()) if path.exists() else {}
    stored[key] = values
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n")


# Reports ---------------------------------------------------------------------------------------


def write_reports(
    rows: Sequence[Graded],
    reports: Mapping[tuple[str, str], PositionReport],
    out_dir: Path,
    *,
    title: str,
    notes: Sequence[str] = (),
    gate_label: str = "close",
) -> list[Path]:
    """`by_position.csv`, `by_week.csv`, `coverage.png` and `summary.md` in `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = [
        _by_position(reports, out_dir / "by_position.csv"),
        _by_week(rows, out_dir / "by_week.csv"),
        _coverage_plot(reports, out_dir / "coverage.png"),
        _summary(reports, out_dir / "summary.md", title, notes, gate_label),
    ]
    return written


def _by_position(reports: Mapping[tuple[str, str], PositionReport], path: Path) -> Path:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        fields = [*PositionReport.__dataclass_fields__, "beats_ecr"]
        writer.writerow(fields)
        for report in reports.values():
            values = {**asdict(report), "beats_ecr": report.beats_ecr}
            writer.writerow([_cell(values[f]) for f in fields])
    return path


def _by_week(rows: Sequence[Graded], path: Path) -> Path:
    groups: dict[tuple[str, int, int, str], list[Graded]] = defaultdict(list)
    for row in rows:
        groups[(row.label, row.season, row.week, row.position)].append(row)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["label", "season", "week", "position", "n", "mae", "mae_ecr", "n_ecr"])
        for (label, season, week, position), group in sorted(groups.items()):
            both = [r for r in group if r.ecr is not None]
            writer.writerow(
                [
                    label,
                    season,
                    week,
                    position,
                    len(group),
                    f"{_mae((r.projected, r.actual) for r in group):.4f}",
                    f"{_mae((r.ecr or 0.0, r.actual) for r in both):.4f}" if both else "",
                    len(both),
                ]
            )
    return path


def _coverage_plot(reports: Mapping[tuple[str, str], PositionReport], path: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot

    items = list(reports.values())
    names = [f"{r.position}\n{r.label}" for r in items]
    xs = list(range(len(items)))
    figure, axis = pyplot.subplots(figsize=(max(5.0, 0.6 * len(items) + 2), 4))
    axis.bar([x - 0.2 for x in xs], [r.below_p20 for r in items], 0.4, label="below p20")
    axis.bar([x + 0.2 for x in xs], [r.below_p80 for r in items], 0.4, label="below p80")
    for target in (0.2, 0.8):
        axis.axhline(target, color="#888", linestyle="--", linewidth=1)
    axis.set_xticks(xs, names, fontsize=8)
    axis.set_ylim(0, 1)
    axis.set_ylabel("share of outcomes")
    axis.set_title("Range coverage (targets 20% and 80%)")
    axis.legend(frameon=False, loc="upper left")
    figure.tight_layout()
    figure.savefig(path, dpi=110)
    pyplot.close(figure)
    return path


def _summary(
    reports: Mapping[tuple[str, str], PositionReport],
    path: Path,
    title: str,
    notes: Sequence[str],
    gate_label: str,
) -> Path:
    lines = [f"# {title}", ""]
    lines += [f"- {note}" for note in notes]
    if notes:
        lines.append("")
    lines += [
        "| label | position | n | MAE | RMSE | bias | Spearman | below p20 | below p80 | "
        "model share | vs ECR (n) | our MAE | ECR MAE | beats ECR |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: "
        "| --- |",
    ]
    for r in reports.values():
        beats = {True: "yes", False: "**no**", None: "-"}[r.beats_ecr]
        lines.append(
            f"| {r.label} | {r.position} | {r.n} | {r.mae:.2f} | {r.rmse:.2f} | {r.bias:+.2f} | "
            f"{_fmt(r.spearman)} | {r.below_p20:.1%} | {r.below_p80:.1%} | {r.model_share:.0%} | "
            f"{r.head_to_head} | {_fmt(r.mae_ours, 2)} | {_fmt(r.mae_ecr, 2)} | {beats} |"
        )
    verdict = gate(reports, gate_label)
    lines += ["", f"## Gate ({gate_label}): beat ECR on MAE at every position", ""]
    if not verdict:
        lines.append(f"No `{gate_label}` rows were graded.")
    for position, won in verdict.items():
        state = {True: "beats ECR", False: "does not beat ECR", None: "no ECR to compare"}[won]
        lines.append(f"- {position}: {state}")
    lines.append("")
    path.write_text("\n".join(lines))
    return path


def _fmt(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)
