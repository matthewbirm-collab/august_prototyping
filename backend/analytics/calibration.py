"""Check the market probabilities and the fitted curves against what actually happened.

Two questions, answered per market:

1. **Is `fair_p_over` calibrated?** Group the closing consensus probabilities into buckets: of the
   props priced near 60%, did about 60% go over? (Brier score and log-loss summarise it.) This
   tests the de-vig, the consensus and any assumed margin.
2. **Are the fitted ranges the right width?** Place each real outcome in the distribution fitted
   to its line (the probability integral transform). If the p20-p80 range is right, 60% of
   outcomes fall inside it, 20% below and 20% above. Too few inside means the shapes are too
   narrow.

Pushes (a whole-number line hit exactly) are left out of the first and counted separately.
"""

import csv
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from engine.distributions import StatModel, fit_anytime_td, fit_line

BUCKETS = 10
MIN_OBSERVATIONS = 30
# A bucket whose observed rate is this many standard errors from its prediction is flagged.
FLAG_Z = 2.5
_EDGE = 1e-6


@dataclass(frozen=True)
class Observation:
    market: str
    p_over: float
    line: float | None
    """None for anytime TD, where "over" means at least one touchdown."""
    actual: float
    model: StatModel | None = None
    """The shape used to fit this line, for the range check; None skips it."""


def went_over(observation: Observation) -> bool | None:
    """True if the stat finished over the line, False if under, None for a push."""
    if observation.line is None:
        return observation.actual >= 1
    if observation.actual > observation.line:
        return True
    if observation.actual < observation.line:
        return False
    return None


@dataclass(frozen=True)
class Bucket:
    low: float
    high: float
    n: int
    predicted: float
    observed: float

    @property
    def z(self) -> float:
        """How many standard errors the observed rate is from the predicted one."""
        spread = math.sqrt(max(self.predicted * (1 - self.predicted), 1e-9) / self.n)
        return (self.observed - self.predicted) / spread


@dataclass(frozen=True)
class RangeCheck:
    n: int
    below_p20: float
    inside_p20_p80: float
    above_p80: float
    mean_position: float
    """Mean place in the distribution: 0.5 if the fitted means are right."""

    @property
    def verdict(self) -> str:
        if self.inside_p20_p80 < 0.55:
            return "ranges too narrow"
        if self.inside_p20_p80 > 0.65:
            return "ranges too wide"
        return "ranges about right"


@dataclass(frozen=True)
class MarketReport:
    market: str
    n: int
    pushes: int
    base_rate: float
    mean_predicted: float
    brier: float
    log_loss: float
    calibration_error: float
    """Average gap between predicted and observed, weighted by bucket size."""
    buckets: tuple[Bucket, ...]
    range_check: RangeCheck | None

    @property
    def flagged_buckets(self) -> int:
        return sum(1 for b in self.buckets if abs(b.z) > FLAG_Z)

    @property
    def enough_data(self) -> bool:
        return self.n >= MIN_OBSERVATIONS


def reliability(observations: Sequence[Observation], buckets: int = BUCKETS) -> list[Bucket]:
    """Equal-count buckets of the predicted probability, with the share that went over."""
    decided = sorted((o.p_over, over) for o in observations if (over := went_over(o)) is not None)
    if not decided:
        return []
    buckets = min(buckets, len(decided))
    result = []
    for i in range(buckets):
        chunk = decided[i * len(decided) // buckets : (i + 1) * len(decided) // buckets]
        result.append(
            Bucket(
                low=chunk[0][0],
                high=chunk[-1][0],
                n=len(chunk),
                predicted=sum(p for p, _ in chunk) / len(chunk),
                observed=sum(1 for _, over in chunk if over) / len(chunk),
            )
        )
    return result


def brier_score(observations: Sequence[Observation]) -> float:
    decided = [(o.p_over, over) for o in observations if (over := went_over(o)) is not None]
    return sum((p - (1.0 if over else 0.0)) ** 2 for p, over in decided) / len(decided)


def log_loss(observations: Sequence[Observation]) -> float:
    decided = [(o.p_over, over) for o in observations if (over := went_over(o)) is not None]
    total = 0.0
    for p, over in decided:
        p = min(max(p, _EDGE), 1 - _EDGE)
        total -= math.log(p if over else 1 - p)
    return total / len(decided)


def range_check(observations: Sequence[Observation]) -> RangeCheck | None:
    """Where the outcomes fall in the distributions fitted to their own lines."""
    positions: list[float] = []
    for o in observations:
        if o.model is None:
            continue
        fitted = fit_anytime_td(o.p_over) if o.line is None else fit_line(o.model, o.line, o.p_over)
        positions.append(fitted.pit(o.actual))
    if not positions:
        return None
    n = len(positions)
    return RangeCheck(
        n=n,
        below_p20=sum(1 for u in positions if u < 0.2) / n,
        inside_p20_p80=sum(1 for u in positions if 0.2 <= u <= 0.8) / n,
        above_p80=sum(1 for u in positions if u > 0.8) / n,
        mean_position=sum(positions) / n,
    )


def evaluate(
    observations: Sequence[Observation], buckets: int = BUCKETS
) -> dict[str, MarketReport]:
    """One report per market, sorted by name. Markets with nothing decided are left out."""
    by_market: dict[str, list[Observation]] = defaultdict(list)
    for observation in observations:
        by_market[observation.market].append(observation)
    reports: dict[str, MarketReport] = {}
    for market in sorted(by_market):
        rows = by_market[market]
        decided = [o for o in rows if went_over(o) is not None]
        if not decided:
            continue
        found = reliability(rows, buckets)
        reports[market] = MarketReport(
            market=market,
            n=len(decided),
            pushes=len(rows) - len(decided),
            base_rate=sum(1 for o in decided if went_over(o)) / len(decided),
            mean_predicted=sum(o.p_over for o in decided) / len(decided),
            brier=brier_score(rows),
            log_loss=log_loss(rows),
            calibration_error=sum(b.n * abs(b.predicted - b.observed) for b in found)
            / len(decided),
            buckets=tuple(found),
            range_check=range_check(decided),
        )
    return reports


# Reports -----------------------------------------------------------------------------------------


def write_reports(reports: dict[str, MarketReport], out_dir: Path) -> list[Path]:
    """Per market a CSV of buckets and a reliability plot, plus summary.csv and summary.md."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for report in reports.values():
        written.append(_write_buckets(report, out_dir / f"{report.market}.csv"))
        written.append(_plot(report, out_dir / f"{report.market}.png"))
    written.append(_write_summary_csv(reports, out_dir / "summary.csv"))
    written.append(_write_summary_md(reports, out_dir / "summary.md"))
    return written


def _write_buckets(report: MarketReport, path: Path) -> Path:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["low", "high", "n", "predicted", "observed", "z"])
        for b in report.buckets:
            writer.writerow(
                [
                    f"{b.low:.4f}",
                    f"{b.high:.4f}",
                    b.n,
                    f"{b.predicted:.4f}",
                    f"{b.observed:.4f}",
                    f"{b.z:.2f}",
                ]
            )
    return path


def _plot(report: MarketReport, path: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot

    figure, (top, bottom) = pyplot.subplots(
        2, 1, figsize=(5.5, 6.5), gridspec_kw={"height_ratios": [4, 1]}, sharex=True
    )
    top.plot(
        [0, 1], [0, 1], color="#888", linestyle="--", linewidth=1, label="perfectly calibrated"
    )
    predicted = [b.predicted for b in report.buckets]
    observed = [b.observed for b in report.buckets]
    errors = [
        1.96 * math.sqrt(max(b.predicted * (1 - b.predicted), 1e-9) / b.n) for b in report.buckets
    ]
    top.errorbar(
        predicted, observed, yerr=errors, fmt="o-", color="#1f77b4", capsize=3, label="observed"
    )
    top.set_ylabel("share that went over")
    top.set_ylim(0, 1)
    top.set_title(
        f"{report.market}: n={report.n}, Brier {report.brier:.3f}, "
        f"mean gap {report.calibration_error:.3f}"
    )
    top.legend(loc="upper left", frameon=False)
    bottom.bar(predicted, [b.n for b in report.buckets], width=0.04, color="#1f77b4")
    bottom.set_xlabel("predicted probability of the over")
    bottom.set_ylabel("props")
    bottom.set_xlim(0, 1)
    figure.tight_layout()
    figure.savefig(path, dpi=110)
    pyplot.close(figure)
    return path


SUMMARY_COLUMNS = [
    "market", "n", "pushes", "base_rate", "mean_predicted", "brier", "log_loss",
    "calibration_error", "flagged_buckets", "below_p20", "inside_p20_p80", "above_p80", "verdict",
]  # fmt: skip


def _summary_row(report: MarketReport) -> list[str]:
    check = report.range_check
    return [
        report.market,
        str(report.n),
        str(report.pushes),
        f"{report.base_rate:.4f}",
        f"{report.mean_predicted:.4f}",
        f"{report.brier:.4f}",
        f"{report.log_loss:.4f}",
        f"{report.calibration_error:.4f}",
        str(report.flagged_buckets),
        f"{check.below_p20:.4f}" if check else "",
        f"{check.inside_p20_p80:.4f}" if check else "",
        f"{check.above_p80:.4f}" if check else "",
        check.verdict if check else "",
    ]


def _write_summary_csv(reports: dict[str, MarketReport], path: Path) -> Path:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(SUMMARY_COLUMNS)
        writer.writerows(_summary_row(r) for r in reports.values())
    return path


def _write_summary_md(reports: dict[str, MarketReport], path: Path) -> Path:
    lines = [
        "# Calibration",
        "",
        "Of the props priced near X%, did about X% go over (`mean gap` is the average difference, "
        "weighted by bucket size; a bucket is flagged when it is more than "
        f"{FLAG_Z} standard errors off)? And do about 60% of outcomes fall between the fitted "
        "p20 and p80?",
        "",
        "| market | n | pushes | went over | predicted | Brier | mean gap | flagged | "
        "below p20 | inside | above p80 | verdict |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in reports.values():
        c = r.range_check
        note = "" if r.enough_data else f" (under {MIN_OBSERVATIONS}: too few to judge)"
        lines.append(
            f"| {r.market}{note} | {r.n} | {r.pushes} | {r.base_rate:.1%} | "
            f"{r.mean_predicted:.1%} | {r.brier:.3f} | {r.calibration_error:.3f} | "
            f"{r.flagged_buckets}/{len(r.buckets)} | "
            + (
                f"{c.below_p20:.1%} | {c.inside_p20_p80:.1%} | {c.above_p80:.1%} | {c.verdict} |"
                if c
                else " |  |  | |"
            )
        )
    lines.append("")
    path.write_text("\n".join(lines))
    return path
