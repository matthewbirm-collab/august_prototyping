---
description: Phase 2 — nflverse loaders via nflreadpy
---
Follow the working agreement in `CLAUDE.md`. Plan: `docs/PLAN.md` §2. Roadmap step: `/p2-nflverse`.

Write loaders in `backend/ingest/nflverse/` using nflreadpy (Polars) for: schedules, weekly rosters, injuries, depth charts, snap counts, player game stats, `load_ff_playerids()` and `load_ff_rankings()`. Upsert each into its table keyed by season/week.

- Filesystem caching with a 6-hour TTL in season.
- Map nflverse game ids to odds event ids by home team, away team and kickoff date; log unmatched games.
- Seed `players` from rosters + ff_playerids so the resolver has candidates.
- CLI: `ingest-nflverse --season 2026 --week 8 --datasets injuries,depth_charts`.
- Add nflverse attribution (CC-BY 4.0; FTN data CC-BY-SA 4.0) to the README and a frontend footer component.
- Tests use small recorded Parquet/CSV fixtures, not the network.
