"""Reproduce the frozen Week 7 WR rankings offline with Python 3.12."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import replace
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from engine.wr_model import Settings, Week, forecast, priors, validate_sample
from ingest.propline_qb import timestamp


def load_rows(path: Path) -> list[Week]:
    with path.open() as stream:
        raw = list(csv.DictReader(stream))
    fields = set(Week.__dataclass_fields__)
    result = []
    for row in raw:
        values: dict[str, Any] = {
            k: v if k in {"player", "player_id", "team"} else int(v)
            for k, v in row.items()
            if k in fields
        }
        result.append(Week(**values))
    return result


def build(
    data: Path, config: dict[str, Any], as_of: str | None = None
) -> dict[str, Any]:
    context = json.loads((data / "context.json").read_text())
    when = timestamp(as_of or context["as_of"])
    if when != timestamp(context["as_of"]):
        raise ValueError(
            "Frozen input supports only its captured as_of; refresh all inputs to rerun"
        )
    cfg = Settings(**config["model"])
    if context["week"] != cfg.cutoff_week + 1:
        raise ValueError("Context week and model cutoff disagree")
    rows = load_rows(data / "weekly-inputs.csv")
    validate_sample(rows, cfg)
    if any(r.week not in context["window"] for r in rows):
        raise ValueError("Rows outside the declared window")
    with (data / "team-dropbacks.csv").open() as stream:
        teams = list(csv.DictReader(stream))
    if len({(t["team"], t["week"]) for t in teams}) != len(teams):
        raise ValueError("Duplicate team-week")
    if any(int(t["week"]) > cfg.cutoff_week for t in teams):
        raise ValueError("Future team stats")
    indexed = {(t["team"], int(t["week"])): t for t in teams}
    for r in rows:
        if int(indexed[r.team, r.week]["dropbacks"]) != r.team_dropbacks:
            raise ValueError("Player and team dropbacks disagree")
    pool = priors(rows, cfg)
    league_db = mean(int(t["dropbacks"]) for t in teams)
    ranked, excluded = [], []
    for pid, info in sorted(context["players"].items()):
        team = info["team"]
        # After a trade, old-team usage is not a forecast of the new role.
        sample = [r for r in rows if r.player_id == pid and r.team == team]
        reason = ""
        if info["status"] in {"out", "bye", "locked"}:
            reason = info["status"]
        elif team not in context["teams"]:
            reason = "no upcoming Week 7 event"
        elif timestamp(context["teams"][team]["kickoff"]) <= when:
            reason = "locked"
        elif sum(r.routes for r in sample) < cfg.min_routes or not sum(
            r.targets for r in sample
        ):
            reason = "insufficient current-team routes/targets"
        team_games = [t for t in teams if t["team"] == team]
        if not reason and max(r.week for r in sample) < max(
            int(t["week"]) for t in team_games
        ):
            reason = "absent from latest team report; role needs verification"
        if reason:
            excluded.append({"player": info["player"], "team": team, "reason": reason})
            continue
        weights = [
            cfg.recency_decay ** (cfg.cutoff_week - int(t["week"])) for t in team_games
        ]
        db = (
            sum(
                int(t["dropbacks"]) * w
                for t, w in zip(team_games, weights, strict=True)
            )
            + cfg.team_prior_games * league_db
        ) / (sum(weights) + cfg.team_prior_games)
        market = info["td_market"].get(str(config["td_probability_multiplier"]))
        if market:
            td = market["td_mean"]
            td_source = "PropLine anytime TD; assumed discount for one-sided prices"
        else:
            implied = context["teams"][team]["implied_points"]
            if implied is None:
                excluded.append(
                    {
                        "player": info["player"],
                        "team": team,
                        "reason": "missing TD inputs",
                    }
                )
                continue
            # Use matching active weeks for share, not a denominator that counts missed games.
            target_share = sum(r.targets for r in sample) / sum(
                int(indexed[team, r.week]["targets"]) for r in sample
            )
            td = (
                implied
                / config["fallback_points_per_td"]
                * config["fallback_td_scoring_fraction"]
                * config["fallback_receiving_td_fraction"]
                * target_share
            )
            td_source = (
                "model: implied team points × assumed TD fractions × target share"
            )
        result = forecast(sample, pool, db, td, config["weights"], cfg)
        scenarios = {}
        for factor in [0.5, 2.0]:
            altered = replace(
                cfg,
                target_prior_exposure=cfg.target_prior_exposure * factor,
                catch_prior_exposure=cfg.catch_prior_exposure * factor,
                yards_prior_receptions=cfg.yards_prior_receptions * factor,
            )
            scenarios[f"prior_{factor}"] = forecast(
                sample, pool, db, td, config["weights"], altered, simulate=False
            )["mean"]
        for factor in config["td_multiplier_scenarios"]:
            scenario_market = info["td_market"].get(str(factor))
            scenarios[f"td_{factor}"] = forecast(
                sample,
                pool,
                db,
                scenario_market["td_mean"] if scenario_market else td,
                config["weights"],
                cfg,
                simulate=False,
            )["mean"]
        for latest_weight in [0.0, 1.0]:
            scenarios[f"role_{latest_weight}"] = forecast(
                sample,
                pool,
                db,
                td,
                config["weights"],
                replace(cfg, role_latest_weight=latest_weight),
                simulate=False,
            )["mean"]
        reduced = forecast(
            sample, pool, db, td, config["weights"], cfg, workload=0.75, simulate=False
        )["mean"]
        ranked.append(
            {
                "player": info["player"],
                "player_id": pid,
                "team": team,
                "opponent": context["teams"][team]["opponent"],
                "availability": info["status"],
                "injury": info["injury"],
                "confidence": "low"
                if len(sample) < 3 or not market or info["status"] == "conditional"
                else "moderate (uncalibrated)",
                "td_source": td_source,
                "scenarios": scenarios,
                "mean_at_75pct_workload": reduced,
                **result,
            }
        )
    ranked.sort(key=lambda r: (-r["mean"], r["player"]))
    for i, row in enumerate(ranked, 1):
        row["rank"] = i
    for key in ["prior_0.5", "prior_2.0", "td_0.85", "td_1.0", "role_0.0", "role_1.0"]:
        order = sorted(ranked, key=lambda r: (-r["scenarios"][key], r["player"]))
        for i, row in enumerate(order, 1):
            row.setdefault("scenario_ranks", {})[key] = i
    for row in ranked:
        rr = [row["rank"], *row["scenario_ranks"].values()]
        row["rank_sensitivity"] = [min(rr), max(rr)]
    return {
        "version": "wr-usage-prototype-1",
        "as_of": context["as_of"],
        "config": config,
        "empirical_priors": pool,
        "input_rows": len(rows),
        "covered_players": len(context["players"]),
        "rankings": ranked,
        "excluded": excluded,
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Week 7 WR rankings — experimental half-PPR",
        "",
        f"Frozen as of **{report['as_of']}**; Weeks 3–6 only.",
        "",
        "Ranks are conditional on playing the modeled workload. **CHECK** means injury/practice",
        "uncertainty: use the 75% workload scenario, not an invented chance of playing.",
        "No designation is not a guarantee of health. Recheck inactives before kickoff.",
        "",
        "P20–P80 and SD are simulated, uncalibrated outcomes—not validated confidence bounds.",
        "Scores omit fumbles, two-point conversions and bonuses. Rushing yards are shrunk means.",
        "Opponent is shown for context; defensive matchup and weather effects are not fitted.",
        "",
        "[Method and sources](MODEL.md) · [Machine-readable results](rankings.json)",
        "",
        "| Rank | Receiver | Team vs | Points | P20–P80 | SD | Routes | Targets | YPRR* | aDOT* | Route%* | Status |",
        "| ---: | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in report["rankings"]:
        status = "CHECK" if r["availability"] == "conditional" else "No flag"
        lines.append(
            f"| {r['rank']} | {r['player']} | {r['team']} vs {r['opponent']} | "
            f"{r['mean']:.1f} | {r['p20']:.1f}–{r['p80']:.1f} | {r['sd']:.1f} | "
            f"{r['projected_routes']:.1f} | {r['projected_targets']:.1f} | "
            f"{r['observed_yprr']:.2f} | {r['adot']:.1f} | "
            f"{100 * r['observed_route_participation']:.1f}% | {status} |"
        )
    lines += [
        "",
        "*Observed weighted-by-denominator totals; forecasts use recency and shrinkage.",
        "",
        "## Sensitivity and availability",
        "",
        "Rank span varies prior strengths and one-sided TD-price discounts; it is not a probability interval.",
        "",
        "| Player | Rank span | Points at 75% workload | Confidence |",
        "| --- | --- | ---: | --- |",
    ]
    for r in report["rankings"]:
        span = r["rank_sensitivity"]
        lines.append(
            f"| {r['player']} | {span[0]}–{span[1]} | {r['mean_at_75pct_workload']:.1f} | {r['confidence']} |"
        )
    lines += [
        "",
        "## Exclusions",
        "",
        "| Player | Team | Reason |",
        "| --- | --- | --- |",
    ]
    for r in report["excluded"]:
        lines.append(f"| {r['player']} | {r['team']} | {r['reason']} |")
    return "\n".join(lines) + "\n"


def evaluate(data: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Small expanding-window diagnostic; no Week 7 market/injury information."""
    rows = load_rows(data / "weekly-inputs.csv")
    with (data / "team-dropbacks.csv").open() as stream:
        teams = list(csv.DictReader(stream))
    base_cfg = Settings(**config["model"])
    weights = {**config["weights"], "rushing_yards": 0.0}
    folds = []
    all_errors, all_baseline_errors = [], []
    for week in [4, 5, 6]:
        cfg = replace(base_cfg, cutoff_week=week - 1)
        training = [r for r in rows if r.week < week]
        team_training = [t for t in teams if int(t["week"]) < week]
        league_db = mean(int(t["dropbacks"]) for t in team_training)
        pool = priors(training, cfg)
        errors, baseline_errors = [], []
        for actual in [r for r in rows if r.week == week]:
            history = [
                r
                for r in training
                if r.player_id == actual.player_id and r.team == actual.team
            ]
            if sum(r.routes for r in history) < cfg.min_routes or not sum(
                r.targets for r in history
            ):
                continue
            tg = [t for t in team_training if t["team"] == actual.team]
            if max(r.week for r in history) < max(int(t["week"]) for t in tg):
                continue
            w = [cfg.recency_decay ** (cfg.cutoff_week - int(t["week"])) for t in tg]
            db = (
                sum(int(t["dropbacks"]) * x for t, x in zip(tg, w, strict=True))
                + cfg.team_prior_games * league_db
            ) / (sum(w) + cfg.team_prior_games)
            projected = forecast(history, pool, db, 0, weights, cfg, simulate=False)[
                "mean"
            ]
            observed = (
                actual.receptions * weights["reception"]
                + actual.receiving_yards * weights["receiving_yards"]
            )
            baseline = mean(
                r.receptions * weights["reception"]
                + r.receiving_yards * weights["receiving_yards"]
                for r in history
            )
            errors.append(abs(projected - observed))
            baseline_errors.append(abs(baseline - observed))
        folds.append(
            {
                "week": week,
                "n": len(errors),
                "model_mae": mean(errors),
                "trailing_mean_mae": mean(baseline_errors),
            }
        )
        all_errors.extend(errors)
        all_baseline_errors.extend(baseline_errors)
    return {
        "scope": "receiving yards + receptions only; no TDs/rushing/injury prediction",
        "selection": "only players appearing in held-out weekly report and meeting prior sample rule",
        "limitation": "3 preliminary folds, selected active sample; no interval or full-score calibration",
        "folds": folds,
        "n": len(all_errors),
        "model_mae": mean(all_errors),
        "trailing_mean_mae": mean(all_baseline_errors),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data", type=Path, default=ROOT / "docs/research/2026-week-7/wr-model"
    )
    parser.add_argument(
        "--config", type=Path, default=ROOT / "scoring_configs/wr_half_ppr.json"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "reports/wr-model")
    parser.add_argument(
        "--as-of", help="Only the frozen snapshot timestamp is supported"
    )
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    report = build(args.data, config, args.as_of)
    report["preliminary_evaluation"] = evaluate(args.data, config)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "rankings.json").write_text(json.dumps(report, indent=2) + "\n")
    (args.output / "RANKINGS.md").write_text(markdown(report))
    for filename in [
        "MODEL.md",
        "weekly-inputs.csv",
        "team-dropbacks.csv",
        "context.json",
        "sources.json",
    ]:
        source = args.data / filename
        if source.exists() and source.resolve() != (args.output / filename).resolve():
            (args.output / filename).write_bytes(source.read_bytes())
    print(
        f"Ranked {len(report['rankings'])}; excluded {len(report['excluded'])}. {args.output}"
    )


if __name__ == "__main__":
    main()
