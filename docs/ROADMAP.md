# Roadmap

Run steps in order. Each step is a slash command. `/build-next` picks the first unchecked step.
Do not start a new phase until the previous phase gate is met.

## Phase 0 — Foundation
- [x] `/p0-scaffold` — monorepo, local Homebrew services, CI, Makefile

**Gate:** `make up` starts all services; CI is green on an empty app.

> Met 2026-10-23: `make up` starts Postgres 18 + TimescaleDB 2.31 and Redis, `/health/ready` returns ok for all three, and CI passed on PR #2.

## Phase 1 — Data model
- [x] `/p1-data-model` — schema, migrations, seed data

**Gate:** migrations run up and down; seed data loads.

> Met 2026-10-23: `tests/db/test_migrations.py` runs base → head → base → head and checks the hypertables and `consensus_hourly`; `alembic check` finds no model drift; `make seed` loads 32 teams, 12 books, 14 markets and 3 scoring configs, and reruns change nothing.

## Phase 2 — Ingestion
- [x] `/p2-odds-adapter` — OddsProvider interface + The Odds API implementation (2026-10-23; tested on synthetic fixtures; record real responses and run `pytest -m live` once `ODDS_API_KEY` is set)
- [x] `/p2-player-resolver` — map sportsbook names to canonical players (2026-10-23; reads the `rosters` table, which `/p2-nflverse` fills)
- [x] `/p2-nflverse` — nflreadpy loaders (2026-10-23; weekly rankings exist 2020–2026, but they're ranks, not points: `/p3-backtest` needs a rank-to-points conversion before comparing MAE)
- [x] `/p2-scheduler` — tiered snapshot capture job (2026-10-24; tested with fake and recorded responses, no live capture yet: that needs `ODDS_API_KEY`. Phase 2 gate still needs `/p2-backfill` and a live week)
- [ ] `/p2-backfill` — historical props for 2023–2025

**Gate:** one full week of snapshots captured with under 2% unmatched players.

## Phase 3 — Projection engine

**Separate approved research prototype:** Patrick approved an isolated QB model
and writing it to `main`. `scripts/qb_model.py` now provides market-fitted
distributions and comparisons using PropLine. This does not complete the phase
gates below: database integration, historical calibration/backtesting, gap filling,
and the production engine remain pending.

- [x] Isolated QB prototype — 4-point passing TD config, PropLine snapshot adapter,
  distribution fits, simulation ranges/SD, Purdy comparison, offline tests and report.
- [x] Separate approved WR research prototype — frozen Week 7 usage rankings,
  shrinkage, TD context, simulation, sensitivity checks and preliminary evaluation.
  This does not complete production integration or historical calibration gates.

- [ ] `/p3-odds-math` — odds conversion and de-vig
- [ ] `/p3-consensus` — multi-book consensus
- [ ] `/p3-distributions` — stat distribution fitting
- [ ] `/p3-projection` — fantasy projection assembly
- [ ] `/p3-gapfill` — fill stats with no market
- [ ] `/p3-backtest` — 2023–2025 backtest harness

**Gate:** backtest beats the consensus-rankings baseline on MAE at every position.

## Phase 4 — Movement analytics
- [ ] `/p4-movement` — deltas and attribution
- [ ] `/p4-steam` — steam and stale-line detection
- [ ] `/p4-context` — join moves to injury/depth/weather news
- [ ] `/p4-summaries` — grounded LLM explanations (optional)

**Gate:** every flagged mover has a named driver prop.

## Phase 5 — API and frontend
- [ ] `/p5-api` — REST API + typed TS client
- [ ] `/p5-board` — projections board
- [ ] `/p5-player-page` — player detail with line history
- [ ] `/p5-compare` — start/sit compare

**Gate:** a user can answer a start/sit question in under 30 seconds.

## Phase 6 — Alerts and operations
- [ ] `/p6-alerts` — users, watchlists, alerts
- [ ] `/p6-observability` — metrics, data-quality checks, admin status
- [ ] `/p6-deploy` — AWS CDK, staging + production

**Gate:** one live Sunday run end to end with no manual steps.

## Open decisions (resolve before Phase 2)
- [x] Personal tool, private beta, or public product? — **Personal tool** (2026-10-23). Revisit `/p6-alerts` (no multi-user auth needed) and `/p6-deploy` (full AWS is likely more than a personal tool needs) before Phase 6.
- [ ] Odds provider access and coverage trial — **PropLine selected for the QB prototype** (Patrick, 2026-10-23), replacing the earlier ScoresAndOdds preference. Free key obtained locally; allowance is 1,000 requests/day and a live NFL smoke test passed. Matt's production ingestion adapter currently uses **The Odds API free Starter plan** (500 credits/month, approximately 15/day). Moving production ingestion to PropLine remains a separate integration step. SportsGameOdds Amateur remains an alternative. PropLine NFL history starts in 2026, so the 2023–2025 backfill source remains unresolved.
- [x] Local services — **Homebrew, not Docker** (2026-10-23). See `/p0-scaffold`.
- [x] Repo — **reuse `august_prototyping`** (2026-10-23); the Express starter is removed in `/p0-scaffold`.
- [ ] Which books count as sharp for consensus weighting
- [ ] Revenue model (free / subscription / affiliate — affiliate brings state gaming-ad rules into scope)
- [ ] v2 priority: league sync or DFS salary integration
