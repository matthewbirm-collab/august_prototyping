"""Run an explicitly experimental QB comparison, offline by default."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from engine.qb_model import compare, fit_count, fit_yards, simulate, summarize
from ingest.propline_qb import PropLineProvider, name_key, normalize, timestamp


def build_report(
    snapshot: dict[str, Any],
    config: dict[str, Any],
    as_of: datetime,
    reference: str,
) -> dict[str, Any]:
    players, metadata = normalize(
        snapshot, as_of, config["stale_minutes"], config["td_probability_multiplier"]
    )
    results: list[dict[str, Any]] = []
    samples: dict[str, list[float]] = {}
    weights = config["weights"]
    for player, stats in sorted(players.items()):
        distributions = {}
        failures = {}
        for stat in weights:
            try:
                observations = stats.get(stat, [])
                distributions[stat] = (
                    fit_yards(observations)
                    if stat.endswith("_yds")
                    else fit_count(observations)
                )
            except ValueError as error:
                failures[stat] = str(error)
        entry = {
            **metadata[player],
            "status": "incomplete" if failures else "experimental",
            "missing_or_unfit": failures,
            "fits": {
                s: {
                    **asdict(d),
                    "mean": d.expected,
                    "books": sorted({o.book for o in stats[s]}),
                    "methods": sorted({o.method for o in stats[s]}),
                }
                for s, d in distributions.items()
            },
        }
        if not failures:
            seed = config["seed"] + int.from_bytes(
                hashlib.sha256(player.encode()).digest()[:4]
            )
            draws = simulate(distributions, weights, config["simulation_draws"], seed)
            samples[player] = draws
            expected = sum(d.expected * weights[s] for s, d in distributions.items())
            entry["points"] = summarize(draws, expected)
            entry["expected_points_by_stat"] = {
                s: round(d.expected * weights[s], 3) for s, d in distributions.items()
            }
        results.append(entry)
    references = [
        p
        for p in samples
        if name_key(reference) in {name_key(alias) for alias in metadata[p]["aliases"]}
    ]
    reference_id = references[0] if len(references) == 1 else None
    for entry in results:
        player = entry["key"]
        if player in samples and reference_id:
            entry.update(compare(samples[player], samples[reference_id]))
    results.sort(
        key=lambda r: r.get("points", {}).get("mean", -float("inf")), reverse=True
    )
    return {
        "model_version": "qb-prototype-1",
        "as_of": as_of.isoformat(),
        "snapshot_captured_at": snapshot["captured_at"],
        "scoring": config,
        "reference": reference,
        "reference_available": reference_id is not None,
        "limitations": [
            "Experimental, not backtested or empirically calibrated; no optimal-lineup claim.",
            "Conditional on playing a normal workload; no injury or early-exit adjustment.",
            "Lognormal yardage shape fitted to market ladders, not historical performance.",
            "One-sided yardage ladders borrow the same book/player/stat's paired-market hold.",
            "Unpaired TD probabilities use the configured discount; fair TD odds are unknown.",
            "Poisson count variance equals its mean; overdispersion is not calibrated.",
            "Stats and opposing players sampled independently; correlations can alter ranges.",
            "No fumble-loss, receiving, or bonus points; no historical rates are available yet.",
            "Missing markets are excluded, not silently filled with zero.",
            "Positive lognormal rushing yards cannot represent negative yardage or a zero mass.",
            "Provider IDs plus unambiguous normalized names join books within an event; no fuzzy matching.",
        ],
        "players": results,
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Experimental QB fantasy comparison",
        "",
        f"Snapshot: {report['snapshot_captured_at']}",
        "",
        "Conditional on a normal workload; excludes lost fumbles. Not backtested.",
        "",
        "| Player | Mean | SD | 60% range | 80% range | P(outscore reference) |",
        "|---|---:|---:|---|---|---:|",
    ]
    for player in report["players"]:
        if "points" not in player:
            continue
        p = player["points"]
        chance = player.get("probability_outscores_reference")
        label = f"{chance:.1%}" if chance is not None else "Unavailable"
        if player["player"].casefold() == report["reference"].casefold():
            label = "Reference"
        lines.append(
            f"| {player['player']} | {p['mean']:.1f} | {p['standard_deviation']:.1f} "
            f"| {p['p20']:.1f}–{p['p80']:.1f} | {p['p10']:.1f}–{p['p90']:.1f} "
            f"| {label} |"
        )
    lines += [
        "",
        f"Reference: {report['reference']}",
        "",
        "## Excluded incomplete players",
        "",
    ]
    for player in report["players"]:
        if player["missing_or_unfit"]:
            lines.append(
                f"- {player['player']}: {json.dumps(player['missing_or_unfit'])}"
            )
    lines += ["", "## Assumptions and limitations", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    if "td_discount_sensitivity" in report:
        lines += [
            "",
            "## Sensitivity to the unknown touchdown margin",
            "",
            "Mean-point ranges below vary the multiplier on one-sided implied TD",
            "probabilities from 0.85 to 1.00. These are assumed scenarios, not",
            "confidence bounds or estimates of the actual sportsbook margin.",
            "",
        ]
        lower = report["td_discount_sensitivity"]["0.85"]
        upper = report["td_discount_sensitivity"]["1.0"]
        for player in report["players"]:
            name = player["player"]
            if name in lower and name in upper:
                lines.append(
                    f"- {name}: {lower[name]['mean']:.1f}–{upper[name]['mean']:.1f}"
                )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fetch", action="store_true", help="Make one live bulk request"
    )
    parser.add_argument(
        "--snapshot", type=Path, default=ROOT / "reports/qb-model/snapshot.json"
    )
    parser.add_argument(
        "--config", type=Path, default=ROOT / "scoring_configs/qb_half_ppr.json"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "reports/qb-model")
    parser.add_argument(
        "--as-of", help="UTC-aware timestamp; default is snapshot capture time"
    )
    parser.add_argument("--reference", default="Brock Purdy")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.fetch:
        key = os.environ.get("PROPLINE_API_KEY", "")
        if not key and (ROOT / ".env").exists():
            for line in (ROOT / ".env").read_text().splitlines():
                if line.startswith("PROPLINE_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("\"'")
        provider = PropLineProvider(
            key, ROOT / "reports/qb-model/quota.json", config["daily_request_budget"]
        )
        snapshot = provider.fetch()
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(json.dumps(snapshot))
    else:
        snapshot = json.loads(args.snapshot.read_text())
    as_of = timestamp(args.as_of or snapshot["captured_at"])
    report = build_report(snapshot, config, as_of, args.reference)
    # Model uncertainty: TD hold cannot be recovered from a single yes price.
    sensitivity = {}
    for multiplier in (0.85, 1.0):
        alternative = build_report(
            snapshot,
            {**config, "td_probability_multiplier": multiplier},
            as_of,
            args.reference,
        )
        sensitivity[str(multiplier)] = {
            p["player"]: p["points"] for p in alternative["players"] if "points" in p
        }
    report["td_discount_sensitivity"] = sensitivity
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    (args.output / "comparison.md").write_text(markdown(report))
    print(markdown(report))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError) as error:
        # No request headers, keys, or URLs containing credentials in errors.
        sys.exit(f"QB model stopped: {error}")
