---
description: Phase 5 — start/sit compare for 2–4 players
---
Follow the working agreement in `CLAUDE.md`. Roadmap step: `/p5-compare`.

Build a compare view for 2–4 players:
- Side-by-side projections and ranges.
- Probability each player outscores the others: simulate from fitted per-stat distributions (10,000 draws, independent for v1) inside `engine/`.
- The driver props behind any gap.
- Scoring-config picker; shareable URL.

**Gate (Phase 5):** a user can answer a start/sit question in under 30 seconds. Describe how you verified it.
