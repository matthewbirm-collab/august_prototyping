---
description: Phase 4 — steam and stale-line detection
---
Follow the working agreement in `CLAUDE.md`. Roadmap step: `/p4-steam`.

Detect two event types from `prop_snapshots`:
1. **Steam:** at least `STEAM_MIN_BOOKS` books move the same player/market in the same direction within `STEAM_WINDOW_MIN` minutes.
2. **Stale line:** one book is more than `STALE_LINE_PROB_PTS` fair-probability points off consensus for longer than `STALE_LINE_MIN` minutes.

Emit to a `market_events` table with timestamps, books involved and magnitude. Tune the defaults on 2023–2025 history, report how often each fires per week, and propose updated defaults for `CLAUDE.md` if the data supports it.
