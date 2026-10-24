---
description: Phase 3 — backtest harness for 2023–2025
---
Follow the working agreement in `CLAUDE.md`. Plan: `docs/PLAN.md` §4 Validation. Roadmap step: `/p3-backtest`.

Build a `backtest` CLI that replays 2023–2025 using historical snapshots at T-24h, T-1h and close.

For each week: compute projections as of each snapshot time using only data available then (no leakage — add a test that proves it), grade against `actual_stats`, and report per position: MAE, RMSE, Spearman rank correlation, and p20/p80 coverage (target 20% / 80%).

Compare against the `load_ff_rankings()` baseline where available. Write a markdown report and CSVs to `reports/backtest/`. Fail the run if any position's MAE regresses more than 5% from the last saved baseline.

**Gate (Phase 3):** the backtest beats the rankings baseline on MAE at every position. If it doesn't, report where it falls short and propose fixes before checking this step off.
