---
description: Phase 4 — attribute market moves to injury, depth-chart and weather news
---
Follow the working agreement in `CLAUDE.md`. Roadmap step: `/p4-context`.

Build `backend/analytics/context.py`. For each significant move or market event, look for injury-report changes, depth-chart changes and weather updates for the player, his team, and his position group on both teams within the preceding `CONTEXT_LOOKBACK_HOURS`.

Attach the best candidate explanation with a confidence level, or `"no public news found"` (itself a signal).

Tests: a teammate ruled out boosts a WR; a QB downgrade lowers all his pass catchers; an unrelated injury on the other team is not attributed.

**Gate (Phase 4, after this step):** every flagged mover has a named driver prop.
