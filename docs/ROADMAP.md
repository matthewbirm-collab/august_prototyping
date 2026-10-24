# Roadmap

Run steps in order. Each step is a slash command. `/build-next` picks the first unchecked step.
Do not start a new phase until the previous phase gate is met.

## Phase 0 — Foundation
- [ ] `/p0-scaffold` — monorepo, local Homebrew services, CI, Makefile

**Gate:** `make up` starts all services; CI is green on an empty app.

## Phase 1 — Data model
- [ ] `/p1-data-model` — schema, migrations, seed data

**Gate:** migrations run up and down; seed data loads.

## Phase 2 — Ingestion
- [ ] `/p2-odds-adapter` — OddsProvider interface + The Odds API implementation
- [ ] `/p2-player-resolver` — map sportsbook names to canonical players
- [ ] `/p2-nflverse` — nflreadpy loaders
- [ ] `/p2-scheduler` — tiered snapshot capture job
- [ ] `/p2-backfill` — historical props for 2023–2025

**Gate:** one full week of snapshots captured with under 2% unmatched players.

## Phase 3 — Projection engine
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
- [ ] Odds provider and plan (after a one-week coverage trial) — no account yet (2026-10-23). Build against recorded fixtures until `/p2-scheduler`; historical props for `/p2-backfill` need a paid plan.
- [x] Local services — **Homebrew, not Docker** (2026-10-23). See `/p0-scaffold`.
- [x] Repo — **reuse `august_prototyping`** (2026-10-23); the Express starter is removed in `/p0-scaffold`.
- [ ] Which books count as sharp for consensus weighting
- [ ] Revenue model (free / subscription / affiliate — affiliate brings state gaming-ad rules into scope)
- [ ] v2 priority: league sync or DFS salary integration
