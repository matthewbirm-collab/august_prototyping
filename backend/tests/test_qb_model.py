"""Offline numerical and boundary tests; synthetic inputs, no live provider calls."""

import json
import math
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import NormalDist, mean
from typing import Any
from unittest.mock import patch

from engine.qb_model import (
    Distribution,
    Observation,
    compare,
    fair_over,
    fit_count,
    fit_yards,
    implied_probability,
    poisson_over,
    simulate,
)
from ingest.propline_qb import PropLineProvider, normalize


class QBModelTests(unittest.TestCase):
    def test_devig(self) -> None:
        self.assertEqual(fair_over(-110, -110), 0.5)
        self.assertAlmostEqual(fair_over(-150, 130) + fair_over(130, -150), 1)
        for invalid in (0, 50, math.nan, math.inf):
            with self.assertRaises(ValueError):
                implied_probability(invalid)

    def test_recovers_known_lognormal_shape(self) -> None:
        mu, sigma = math.log(240), 0.28
        observations = [
            Observation("book", line, 1 - NormalDist(mu, sigma).cdf(math.log(line)), "synthetic")
            for line in (150.5, 200.5, 250.5, 300.5, 350.5)
        ]
        fit = fit_yards(observations)
        self.assertAlmostEqual(fit.location, mu)
        self.assertAlmostEqual(fit.shape, sigma)
        self.assertGreater(fit.expected, 240)

    def test_single_line_cannot_identify_variance(self) -> None:
        with self.assertRaises(ValueError):
            fit_yards([Observation("book", 240.5, 0.5, "synthetic")])

    def test_recovers_poisson_and_anytime_td_rate(self) -> None:
        for rate in (0.15, 0.8, 2.5):
            observations = [
                Observation("book", x, poisson_over(rate, x), "synthetic") for x in (0.5, 1.5, 2.5)
            ]
            self.assertAlmostEqual(fit_count(observations).location, rate)
        self.assertAlmostEqual(
            fit_count([Observation("book", 0.5, 0.4, "test")]).location, -math.log(0.6)
        )

    def test_scoring_and_reproducibility(self) -> None:
        distributions = {"pass_td": Distribution("poisson", 2)}
        four = simulate(distributions, {"pass_td": 4}, 20000, 10)
        six = simulate(distributions, {"pass_td": 6}, 20000, 10)
        self.assertEqual(four, simulate(distributions, {"pass_td": 4}, 20000, 10))
        self.assertAlmostEqual(mean(six), 1.5 * mean(four))
        self.assertAlmostEqual(mean(four), 8, delta=0.15)
        with self.assertRaises(ValueError):
            simulate(distributions, {"pass_td": 4, "rush_td": 6}, 1000, 1)

    def test_comparison_ties_are_not_wins(self) -> None:
        result = compare([1, 2, 3], [2, 2, 2])
        self.assertAlmostEqual(result["probability_outscores_reference"], 1 / 3, places=4)
        self.assertAlmostEqual(result["probability_ties_reference"], 1 / 3, places=4)

    def test_snapshot_boundary_and_pairing(self) -> None:
        now = datetime(2026, 10, 24, tzinfo=UTC)
        fresh = now.isoformat()
        outcomes = [
            {"description": "Test QB", "name": "Over", "point": 200.5, "price": -110},
            {"description": "Test QB", "name": "Under", "point": 200.5, "price": -110},
            {"description": "Test QB", "name": "250+ Passing Yards", "price": 150},
        ]
        snapshot = {
            "captured_at": fresh,
            "events": [
                {
                    "id": "one",
                    "away_team": "A",
                    "home_team": "B",
                    "commence_time": (now + timedelta(days=1)).isoformat(),
                    "bookmakers": [
                        {
                            "key": "draftkings",
                            "markets": [
                                {
                                    "key": "player_pass_yds",
                                    "last_update": fresh,
                                    "outcomes": outcomes,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        players, _ = normalize(snapshot, now, 90, 0.9)
        obs = players["one:name:testqb"]["player_pass_yds"]
        self.assertEqual(len(obs), 2)
        self.assertEqual(obs[0].probability, 0.5)
        self.assertEqual(obs[1].line, 249.5)
        with self.assertRaises(ValueError):
            normalize(snapshot, now - timedelta(seconds=1), 90, 0.9)
        self.assertEqual(normalize(snapshot, now + timedelta(hours=2), 90, 0.9)[0], {})
        self.assertEqual(normalize(snapshot, now + timedelta(days=2), 90, 0.9)[0], {})

    def test_quota_blocks_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "quota.json"
            ledger.write_text(json.dumps({"date": datetime.now(UTC).date().isoformat(), "used": 2}))
            provider = PropLineProvider("test-only-key", ledger, budget=2)
            with patch("ingest.propline_qb.urlopen") as network:
                with self.assertRaisesRegex(ValueError, "budget exhausted"):
                    provider.fetch()
                network.assert_not_called()

    def test_failed_request_still_reserves_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "quota.json"
            provider = PropLineProvider("test-only-key", ledger)
            with (
                patch("ingest.propline_qb.urlopen", side_effect=OSError("offline")),
                self.assertRaises(OSError),
            ):
                provider.fetch()
            self.assertEqual(json.loads(ledger.read_text())["used"], 1)
            self.assertNotIn("test-only-key", ledger.read_text())

    def test_aliases_merge_and_future_quotes_are_excluded(self) -> None:
        now = datetime(2026, 10, 24, tzinfo=UTC)
        books: list[dict[str, Any]] = []
        for book, name in [("draftkings", "Test QB"), ("bovada", "Test QB (SF)")]:
            books.append(
                {
                    "key": book,
                    "markets": [
                        {
                            "key": "player_pass_yds",
                            "last_update": now.isoformat(),
                            "outcomes": [
                                {
                                    "description": name,
                                    "player_id": "test:1",
                                    "name": side,
                                    "point": 200.5,
                                    "price": -110,
                                }
                                for side in ("Over", "Under")
                            ],
                        }
                    ],
                }
            )
        snapshot = {
            "captured_at": now.isoformat(),
            "events": [
                {
                    "id": "one",
                    "away_team": "A",
                    "home_team": "B",
                    "bookmakers": books,
                    "commence_time": (now + timedelta(days=1)).isoformat(),
                }
            ],
        }
        players, _ = normalize(snapshot, now, 90, 0.9)
        self.assertEqual(list(players), ["one:test:1"])
        self.assertEqual(len(players["one:test:1"]["player_pass_yds"]), 2)
        books[1]["markets"][0]["last_update"] = (now + timedelta(minutes=1)).isoformat()
        players, _ = normalize(snapshot, now, 90, 0.9)
        self.assertEqual(len(players["one:test:1"]["player_pass_yds"]), 1)

    def test_unanchored_ladder_is_not_treated_as_fair(self) -> None:
        now = datetime(2026, 10, 24, tzinfo=UTC)
        snapshot = {
            "captured_at": now.isoformat(),
            "events": [
                {
                    "id": "one",
                    "away_team": "A",
                    "home_team": "B",
                    "commence_time": (now + timedelta(days=1)).isoformat(),
                    "bookmakers": [
                        {
                            "key": "draftkings",
                            "markets": [
                                {
                                    "key": "player_pass_yds",
                                    "last_update": now.isoformat(),
                                    "outcomes": [
                                        {
                                            "description": "QB",
                                            "name": "250+ Passing Yards",
                                            "price": 120,
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        self.assertEqual(normalize(snapshot, now, 90, 0.9)[0], {})


if __name__ == "__main__":
    unittest.main()
