---
description: Phase 3 — assemble fantasy projections from fitted distributions
---
Follow the working agreement in `CLAUDE.md`. Plan: `docs/PLAN.md` §4 steps 4–5. Roadmap step: `/p3-projection`.

Build `backend/engine/projection.py`. Given a player's fitted stat distributions and a scoring config, return expected fantasy points, p20, p80, a per-stat breakdown and a confidence score.

Rules (also in `CLAUDE.md`):
- Use stat means, not lines.
- RB/WR/TE total TDs from anytime TD only. QBs: pass TD props + anytime TD for rushing scores.
- Fumbles lost from the player's trailing historical rate.
- Confidence penalizes missing markets, high dispersion, few books and stale data.
- Weights only from `scoring_configs`. Test that a custom config (6-pt passing TD, TE premium) changes outputs correctly.

Add a service in `app/` or `ingest/` (not `engine/`) that loads inputs, calls the engine, and writes versioned rows to `projections` with `computed_at` and `as_of`. Wire it to the recompute hook from `/p2-scheduler`.
