"""Experimental usage-based WR forecasts. Pure calculation; no I/O.

Empirical depth-group priors are estimated from the supplied pre-cutoff sample.
Prior strengths and simulation dispersion are assumptions, not fitted parameters.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Any

from engine.qb_model import Distribution


@dataclass(frozen=True)
class Week:
    player: str
    player_id: str
    team: str
    week: int
    routes: int
    team_dropbacks: int
    targets: int
    receptions: int
    receiving_yards: int
    air_yards: int
    receiving_tds: int
    rushing_yards: int
    rushing_tds: int

    def __post_init__(self) -> None:
        if not 0 <= self.receptions <= self.targets <= self.routes <= self.team_dropbacks:
            raise ValueError(f"Inconsistent opportunity counts: {self.player}")
        if self.receiving_tds < 0 or self.rushing_tds < 0 or self.team_dropbacks <= 0:
            raise ValueError("Invalid TD or dropback counts")


@dataclass(frozen=True)
class Settings:
    cutoff_week: int = 6
    recency_decay: float = 0.8
    role_latest_weight: float = 0.6
    route_prior_exposure: float = 20.0
    route_prior_mean: float = 0.65
    target_prior_exposure: float = 50.0
    catch_prior_exposure: float = 20.0
    yards_prior_receptions: float = 10.0
    team_prior_games: float = 2.0
    rushing_prior_games: float = 3.0
    dropback_shape: float = 25.0
    catch_yard_shape: float = 2.0
    draws: int = 10000
    seed: int = 2016
    min_routes: int = 20

    def __post_init__(self) -> None:
        values = [
            self.route_prior_exposure,
            self.target_prior_exposure,
            self.catch_prior_exposure,
            self.yards_prior_receptions,
            self.team_prior_games,
            self.rushing_prior_games,
            self.dropback_shape,
            self.catch_yard_shape,
        ]
        if not all(math.isfinite(x) and x > 0 for x in values):
            raise ValueError("Prior strengths and dispersion must be positive and finite")
        if not 0 < self.recency_decay <= 1 or not 0 < self.route_prior_mean < 1:
            raise ValueError("Invalid recency or route prior")
        if not 0 <= self.role_latest_weight <= 1:
            raise ValueError("Invalid latest-role weight")
        if self.draws < 100 or self.min_routes < 1 or not 1 <= self.cutoff_week <= 18:
            raise ValueError("Invalid sample settings")


def depth_group(adot: float) -> int:
    return 0 if adot < 7 else (1 if adot < 12 else 2)


def validate_sample(rows: list[Week], cfg: Settings) -> None:
    if not rows or any(not 1 <= r.week <= cfg.cutoff_week for r in rows):
        raise ValueError("Empty sample or future-week leakage")
    if len({(r.player_id, r.week) for r in rows}) != len(rows):
        raise ValueError("Duplicate player-week")


def priors(rows: list[Week], cfg: Settings) -> dict[str, Any]:
    validate_sample(rows, cfg)
    groups: dict[int, list[Week]] = {0: [], 1: [], 2: []}
    for pid in {r.player_id for r in rows}:
        player = [r for r in rows if r.player_id == pid]
        targets = sum(r.targets for r in player)
        if targets:
            group = depth_group(sum(r.air_yards for r in player) / targets)
            groups[group].extend(player)
    routes = sum(r.routes for r in rows)
    targets = sum(r.targets for r in rows)
    if not routes or not targets:
        raise ValueError("Sample needs routes and targets")
    depth = {}
    for group, sample in groups.items():
        if sum(r.receptions for r in sample) == 0:
            sample = rows
        t, c = sum(r.targets for r in sample), sum(r.receptions for r in sample)
        if not c:
            raise ValueError("Sample needs receptions")
        depth[group] = {
            "catch_rate": min(0.95, max(0.05, c / t)),
            "yards_per_catch": max(1.0, sum(r.receiving_yards for r in sample) / c),
        }
    return {"tprr": min(0.95, max(0.01, targets / routes)), "depth": depth}


def forecast(
    rows: list[Week],
    pool: dict[str, Any],
    team_dropbacks: float,
    td_mean: float,
    weights: dict[str, float],
    cfg: Settings,
    *,
    workload: float = 1.0,
    simulate: bool = True,
) -> dict[str, Any]:
    validate_sample(rows, cfg)
    if len({r.player_id for r in rows}) != 1:
        raise ValueError("Forecast accepts one player")
    if not math.isfinite(team_dropbacks) or not 0 < team_dropbacks <= 100:
        raise ValueError("Invalid team dropbacks")
    if not math.isfinite(td_mean) or td_mean < 0 or not 0 < workload <= 1:
        raise ValueError("Invalid TD mean or workload")
    if set(weights) != {"reception", "receiving_yards", "rushing_yards", "total_tds"}:
        raise ValueError("Unexpected scoring fields")
    if not all(math.isfinite(v) for v in weights.values()):
        raise ValueError("Nonfinite scoring weights")

    def total(field: str) -> float:
        return sum(float(getattr(r, field)) for r in rows)

    def weighted(field: str) -> float:
        return sum(
            float(getattr(r, field)) * cfg.recency_decay ** (cfg.cutoff_week - r.week) for r in rows
        )

    n_routes, n_targets = total("routes"), total("targets")
    if n_routes < cfg.min_routes or not n_targets:
        raise ValueError("Insufficient route/target sample")
    adot = total("air_yards") / n_targets
    base = pool["depth"][depth_group(adot)]
    latest = max(rows, key=lambda r: r.week)
    exposure = weighted("team_dropbacks")
    history_role = weighted("routes") / exposure
    role = (
        cfg.role_latest_weight * latest.routes / latest.team_dropbacks
        + (1 - cfg.role_latest_weight) * history_role
    )
    route_a = exposure * role + cfg.route_prior_exposure * cfg.route_prior_mean
    route_b = exposure * (1 - role) + cfg.route_prior_exposure * (1 - cfg.route_prior_mean)
    target_a = weighted("targets") + cfg.target_prior_exposure * pool["tprr"]
    target_b = (
        weighted("routes") - weighted("targets") + cfg.target_prior_exposure * (1 - pool["tprr"])
    )
    catch_a = weighted("receptions") + cfg.catch_prior_exposure * base["catch_rate"]
    catch_b = (
        weighted("targets")
        - weighted("receptions")
        + cfg.catch_prior_exposure * (1 - base["catch_rate"])
    )
    ypc = max(
        0.1,
        (weighted("receiving_yards") + cfg.yards_prior_receptions * base["yards_per_catch"])
        / (weighted("receptions") + cfg.yards_prior_receptions),
    )
    route_p, target_p, catch_p = (
        route_a / (route_a + route_b),
        target_a / (target_a + target_b),
        catch_a / (catch_a + catch_b),
    )
    routes = team_dropbacks * route_p * workload
    targets = routes * target_p
    receptions = targets * catch_p
    yards = receptions * ypc
    rush = total("rushing_yards") / (len(rows) + cfg.rushing_prior_games) * workload
    tds = td_mean * workload
    components = {
        "receptions": receptions * weights["reception"],
        "receiving_yards": yards * weights["receiving_yards"],
        "rushing_yards": rush * weights["rushing_yards"],
        "total_tds": tds * weights["total_tds"],
    }
    result: dict[str, Any] = {
        "games": len(rows),
        "sample_routes": int(n_routes),
        "observed_route_participation": n_routes / total("team_dropbacks"),
        "observed_tprr": n_targets / n_routes,
        "observed_yprr": total("receiving_yards") / n_routes,
        "adot": adot,
        "projected_routes": routes,
        "projected_targets": targets,
        "projected_receptions": receptions,
        "projected_receiving_yards": yards,
        "projected_total_tds": tds,
        "shrunk_route_participation": route_p,
        "shrunk_yprr": target_p * catch_p * ypc,
        "mean": sum(components.values()),
        "components": components,
    }
    if simulate:
        seed = cfg.seed + int.from_bytes(hashlib.sha256(rows[0].player_id.encode()).digest()[:4])
        rng = random.Random(seed)  # noqa: S311 - reproducible simulation
        draws = []
        for _ in range(cfg.draws):
            intensity = rng.gammavariate(cfg.dropback_shape, team_dropbacks / cfg.dropback_shape)
            db = int(Distribution("poisson", intensity).sample(rng))
            nr = rng.binomialvariate(db, rng.betavariate(route_a, route_b) * workload)
            nt = rng.binomialvariate(nr, rng.betavariate(target_a, target_b))
            nc = rng.binomialvariate(nt, rng.betavariate(catch_a, catch_b))
            ny = (
                rng.gammavariate(nc * cfg.catch_yard_shape, ypc / cfg.catch_yard_shape) if nc else 0
            )
            td = Distribution("poisson", tds).sample(rng)
            draws.append(
                nc * weights["reception"]
                + ny * weights["receiving_yards"]
                + rush * weights["rushing_yards"]
                + td * weights["total_tds"]
            )
        draws.sort()
        result.update(
            sd=pstdev(draws),
            p20=draws[int(0.2 * (len(draws) - 1))],
            p80=draws[int(0.8 * (len(draws) - 1))],
            simulation_mean=mean(draws),
        )
    return result
