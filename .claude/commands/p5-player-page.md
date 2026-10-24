---
description: Phase 5 — player detail page with line history
---
Follow the working agreement in `CLAUDE.md`. Roadmap step: `/p5-player-page`.

Build `/players/[id]`:
- Header: projection, range, confidence.
- Per-stat breakdown table: line, fair probability, projected mean, fantasy-point contribution, source (market/model).
- Line-movement chart per market: one series per book plus consensus, with injury and depth-chart events marked on the time axis.
- Projection history across the week.
- Grounded movement summary from `/p4-summaries` when available.

Use a time-series-friendly chart library (Recharts or visx). Charts must be readable in light and dark mode.
