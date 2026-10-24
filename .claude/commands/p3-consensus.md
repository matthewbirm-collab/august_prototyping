---
description: Phase 3 — combine books into one fair line and P(over)
---
Follow the working agreement in `CLAUDE.md`. Plan: `docs/PLAN.md` §4 step 2. Roadmap step: `/p3-consensus`.

Build `backend/engine/consensus.py`. Input: all quotes for one (player, market) as of a timestamp. Output: `fair_line`, `fair_p_over`, `books_count`, `dispersion`.

Steps:
1. Drop quotes older than `STALE_QUOTE_MINUTES` relative to the freshest book.
2. De-vig each book (`engine/odds.py`).
3. When books post different lines, translate each to a common reference line using the fitted distribution's slope. Stub with a linear approximation until `/p3-distributions` exists, and leave a TODO pointing to it.
4. Combine with weight = `books.sharpness_weight` × recency decay.
5. Return dispersion (weighted std of fair lines) as a disagreement signal.

Add a job that writes `consensus_snapshots` after each ingestion run.

Tests with hand-built fixtures: unanimous books, one outlier, books on different lines, one stale book.
