---
description: Phase 5 — projections board (main page)
---
Follow the working agreement in `CLAUDE.md`. Roadmap step: `/p5-board`.

Build the main Next.js page: a sortable, filterable projections table for the current week.

- Columns: player, team, opponent, position, projection, p20–p80 range bar, change since open (colored arrow), confidence, last updated.
- Filters: position, scoring config, minimum confidence, search.
- Row click opens the player page.
- Server-render the first load; poll every 60 seconds only within 3 hours of kickoff.
- Mobile layout collapses rows to cards.
- Footer: nflverse attribution and "Projections only — not betting advice."
