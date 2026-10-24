---
description: Phase 1 — Postgres/TimescaleDB schema, Alembic migrations and seed data
---
Follow the working agreement in `CLAUDE.md`. Plan: `docs/PLAN.md` §3. Roadmap step: `/p1-data-model`.

Design the schema and implement it with Alembic migrations. Propose a Mermaid ERD first and wait for approval.

Entities:
- `players` (canonical id, name, position, team, nflverse gsis_id, other external ids)
- `player_aliases` (source, raw_name, player_id) for sportsbook name matching
- `teams`; `games` (season, week, kickoff_utc, home, away, odds_event_id)
- `books` (key, display name, sharpness_weight)
- `markets` (key such as `player_rush_yds`, stat it maps to, distribution family, is_alternate)
- `prop_snapshots` as a TimescaleDB hypertable: captured_at, game_id, book_id, market_id, player_id, line, over_price, under_price, source (live/historical), payload_hash
- `consensus_snapshots`: captured_at, player_id, market_id, fair_line, fair_p_over, books_count, dispersion
- `projections`: computed_at, as_of, player_id, game_id, scoring_config_id, mean, p20, p80, breakdown (JSONB), confidence
- `injuries`, `depth_charts` (weekly), `actual_stats` (for grading)
- `scoring_configs` (named weight sets)
- `ingestion_runs`, `unmatched_names`

Requirements:
- Indexes for "latest snapshot per player/market/book" and "time series for one player/market".
- A continuous aggregate for hourly consensus.
- Skip identical consecutive snapshots using payload_hash.
- Seed data: US books, fantasy-relevant markets, and the PPR / half-PPR / standard scoring configs (weights from `docs/PLAN.md` §4).
- Tests that run migrations up and down against a test database.

**Gate (Phase 1):** migrations run up and down cleanly; `make seed` loads seed data.
