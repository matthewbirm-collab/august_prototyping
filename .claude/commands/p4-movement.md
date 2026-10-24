---
description: Phase 4 — projection deltas and per-prop attribution
---
Follow the working agreement in `CLAUDE.md`. Plan: `docs/PLAN.md` §4 Movement signals. Roadmap step: `/p4-movement`.

Build `backend/analytics/movement.py` and a job that runs after each projection recompute.

Per player and scoring config: delta since open, deltas over 1h / 6h / 24h, velocity (points per hour), and attribution of each delta to individual markets (which prop moved and its fantasy-point effect).

Per market: line delta, `fair_p_over` delta, and the share of books moving the same direction.

Store in a `movement` table and expose a query for top N risers and fallers this week, filterable by position and minimum confidence.
