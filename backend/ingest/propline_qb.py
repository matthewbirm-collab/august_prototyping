"""PropLine snapshot boundary for the isolated QB research prototype."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.request import Request, urlopen

from engine.qb_model import Observation, fair_over, implied_probability

MARKETS = (
    "player_pass_yds",
    "player_pass_tds",
    "player_pass_interceptions",
    "player_rush_yds",
    "player_anytime_td",
)


class OddsProvider(Protocol):
    def fetch(self) -> dict[str, Any]: ...


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return parsed.astimezone(UTC)


def clean_name(value: str) -> str:
    return re.sub(r"\s+\([A-Z]{2,3}\)$", "", value).strip()


def name_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", clean_name(value).casefold())


class PropLineProvider:
    """One bulk request; reserve local daily budget before network access.

    The local ledger serializes this client's usage, and syncs server usage when
    available. Calls by other clients are ultimately limited by the provider.
    """

    def __init__(self, key: str, ledger: Path, budget: int = 1000):
        if not key or not 1 <= budget <= 1000:
            raise ValueError("A key and a budget between 1 and 1000 are required")
        self.key, self.ledger, self.budget = key, ledger, budget

    def fetch(self) -> dict[str, Any]:
        import fcntl  # POSIX local prototype (macOS/Linux)

        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a+") as ledger:
            fcntl.flock(ledger, fcntl.LOCK_EX)
            ledger.seek(0)
            state = json.loads(ledger.read() or "{}")
            today = datetime.now(UTC).date().isoformat()
            used = int(state.get("used", 0)) if state.get("date") == today else 0
            if used >= self.budget:
                raise ValueError("Daily PropLine request budget exhausted")

            def save(count: int) -> None:
                ledger.seek(0)
                ledger.truncate()
                json.dump({"date": today, "used": count}, ledger)
                ledger.flush()

            save(used + 1)
            url = "https://api.prop-line.com/v1/sports/football_nfl/odds?markets="
            url += ",".join(MARKETS) + "&bookmakers=draftkings,fanduel,bovada"
            request = Request(url, headers={"X-API-Key": self.key})  # noqa: S310 - fixed HTTPS host
            with urlopen(request, timeout=45) as response:  # noqa: S310 - fixed HTTPS host
                events = json.load(response)
                used = max(used + 1, int(response.headers.get("X-Daily-Used", used + 1)))
                save(used)
                return {
                    "captured_at": datetime.now(UTC).isoformat(),
                    "events": events,
                    "daily_used": used,
                    "provider": "PropLine",
                }


def normalize(
    snapshot: dict[str, Any],
    as_of: datetime,
    stale_minutes: int,
    td_probability_multiplier: float,
) -> tuple[dict[str, dict[str, list[Observation]]], dict[str, dict[str, Any]]]:
    """Keep pregame, fresh quotes known at as_of; pair prices within each book.

    One-sided ladder odds borrow a constant proportional hold from that book's
    paired market for the SAME player/stat. This is an approximation, not a
    directly observed fair tail. Unanchored yardage ladders are discarded.
    """
    if as_of.tzinfo is None or timestamp(snapshot["captured_at"]) > as_of:
        raise ValueError("Snapshot was not available at the requested as_of time")
    if stale_minutes <= 0 or not 0 < td_probability_multiplier <= 1:
        raise ValueError("Invalid freshness or TD probability settings")
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    metadata: dict[str, dict[str, Any]] = {}
    for event in snapshot["events"]:
        if event.get("live") or timestamp(event["commence_time"]) <= as_of:
            continue
        event_id = str(event["id"])
        # Resolve provider IDs within this snapshot, with exact normalized-name
        # fallback only when it maps to one ID. Never fuzzy-merge player names.
        ids_by_name: dict[str, set[str]] = defaultdict(set)
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    if outcome.get("description") and outcome.get("player_id"):
                        ids_by_name[name_key(outcome["description"])].add(outcome["player_id"])
        for book in event.get("bookmakers", []):
            if book["key"] not in {"draftkings", "fanduel", "bovada"}:
                continue
            for market in book.get("markets", []):
                if market.get("key") not in MARKETS or market.get("period"):
                    continue
                for outcome in market.get("outcomes", []):
                    name = outcome.get("description")
                    price = outcome.get("price")
                    if not name or not isinstance(price, (float, int)):
                        continue
                    if outcome.get("dfs_odds_type") or market.get("suspended_at"):
                        continue
                    # Use observed availability, not last price change: an unchanged
                    # quote can remain fresh. Never use future observations.
                    observed = outcome.get("last_seen_at") or market.get("last_update")
                    if not observed:
                        continue
                    when = timestamp(observed)
                    if not as_of - timedelta(minutes=stale_minutes) <= when <= as_of:
                        continue
                    try:
                        implied_probability(price)
                    except ValueError:
                        continue
                    candidates = ids_by_name[name_key(name)]
                    identity = outcome.get("player_id")
                    if not identity and len(candidates) == 1:
                        identity = next(iter(candidates))
                    if not identity and len(candidates) > 1:
                        continue
                    identity = identity or "name:" + name_key(name)
                    player = event_id + ":" + identity
                    metadata.setdefault(
                        player,
                        {
                            "player": clean_name(name),
                            "key": player,
                            "provider_player_id": outcome.get("player_id"),
                            "aliases": [],
                            "event_id": event_id,
                            "game": event["away_team"] + " at " + event["home_team"],
                            "kickoff": event["commence_time"],
                        },
                    )
                    if clean_name(name) not in metadata[player]["aliases"]:
                        metadata[player]["aliases"].append(clean_name(name))
                    groups[player, book["key"], market["key"]].append(outcome)
    result: dict[str, dict[str, list[Observation]]] = {}
    for (player, book, stat), outcomes in groups.items():
        observations: dict[float, Observation] = {}
        paired: dict[float, dict[str, float]] = defaultdict(dict)
        for o in outcomes:
            point = o.get("point")
            if (
                o["name"] in {"Over", "Under"}
                and isinstance(point, (float, int))
                and math.isfinite(point)
                and point % 1 == 0.5
            ):
                paired[point][o["name"]] = o["price"]
        holds: list[float] = []
        for point, sides in paired.items():
            if set(sides) == {"Over", "Under"}:
                hold = sum(implied_probability(x) for x in sides.values())
                if not 0.95 <= hold <= 1.3:
                    continue
                holds.append(hold)
                observations[point] = Observation(
                    book,
                    point,
                    fair_over(sides["Over"], sides["Under"]),
                    "two_way_devig",
                )
        if holds and stat.endswith("_yds"):
            hold = sum(holds) / len(holds)
            for o in outcomes:
                match = re.match(r"^(\d+)\+\s", o["name"])
                if match:
                    threshold = float(match[1]) - 0.5
                    p = implied_probability(o["price"]) / hold
                    if threshold > 0 and 0 < p < 1:
                        observations.setdefault(
                            threshold,
                            Observation(
                                book,
                                threshold,
                                p,
                                "one_sided_ladder_estimated_hold",
                            ),
                        )
        if stat == "player_anytime_td":
            # A scorer list is not mutually exclusive: never normalize it across players.
            yes = [
                o
                for o in outcomes
                if o["name"] == "Yes" or name_key(o["name"]) == name_key(o["description"])
            ]
            no = [o for o in outcomes if o["name"] == "No"]
            if yes:
                p = (
                    fair_over(yes[0]["price"], no[0]["price"])
                    if no
                    else (implied_probability(yes[0]["price"]) * td_probability_multiplier)
                )
                observations[0.5] = Observation(
                    book, 0.5, p, "two_way_devig" if no else "one_sided_td_assumed_discount"
                )
        if observations:
            result.setdefault(player, {})[stat] = list(observations.values()) + (
                result.get(player, {}).get(stat, [])
            )
    # Passing-yard markets identify QBs; avoid ranking RBs/WRs by rushing alone.
    return {p: stats for p, stats in result.items() if "player_pass_yds" in stats}, metadata
