---
description: Phase 2 — backfill historical prop snapshots for 2023–2025
---
Follow the working agreement in `CLAUDE.md`. Roadmap step: `/p2-backfill`.

Write a `backfill-props` command that pulls historical prop snapshots for the 2023, 2024 and 2025 regular seasons.

- Per game, capture: open (first available), T-72h, T-24h, T-6h, T-1h, and close (last before kickoff).
- Estimate total credit cost and print it; require `--confirm` before spending anything.
- Resumable: track completed (game, timestamp) pairs and skip them on rerun.
- Store in `prop_snapshots` with `source="historical"`, going through the same resolver.
- Load matching `actual_stats` for those games from nflverse.

Report the unmatched-player rate per season when done.
