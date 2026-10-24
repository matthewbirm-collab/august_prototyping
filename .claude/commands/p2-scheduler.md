---
description: Phase 2 — tiered snapshot capture job
---
Follow the working agreement in `CLAUDE.md`. Roadmap step: `/p2-scheduler`.

Build the ingestion job that captures prop snapshots on a tiered schedule.

- Polling tiers by time to kickoff come from the defaults table in `CLAUDE.md` (configurable); stop at kickoff.
- Each run: list this week's events → request props per event → normalize → resolve players → insert into `prop_snapshots`, skipping unchanged quotes → enqueue a consensus/projection recompute for affected players (a no-op hook until Phase 3).
- Record each run in `ingestion_runs`: start, end, events, quotes, new snapshots, credits used, errors.
- Local: APScheduler. Production: a CLI entry point (`run-ingest --tier auto`) that EventBridge Scheduler can invoke on ECS.
- Before the first run, print the projected weekly credit spend for the current tier config.

Tests: tier selection, dedupe, and partial failure (one event errors, the rest still store).

**Gate (Phase 2, after `/p2-backfill`):** one full week of snapshots captured with under 2% unmatched players.
