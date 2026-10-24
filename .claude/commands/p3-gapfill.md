---
description: Phase 3 — fill stats that have no prop market
---
Follow the working agreement in `CLAUDE.md`. Plan: `docs/PLAN.md` §4 step 6. Roadmap step: `/p3-gapfill`.

Build `backend/engine/gapfill.py` for players missing props on some stats (backup RBs, WR3s, QB rushing yards).

- Team implied points from the game spread and total.
- Team plays, pass/rush split and red-zone share from trailing nflverse data.
- Allocate to players by trailing 4-week usage shares (targets, carries, snap share), adjusted for this week's injuries and depth-chart changes.
- Fill only stats with no market; mark them `source="model"` and lower confidence.

Tests: a player with full props is untouched; shares on one team never exceed 100%; a WR1 ruled out redistributes targets.
