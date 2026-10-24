---
description: Phase 3 — fit stat distributions to consensus lines
---
Follow the working agreement in `CLAUDE.md`. Plan: `docs/PLAN.md` §4 step 3. Roadmap step: `/p3-distributions`.

Build `backend/engine/distributions.py`. For each market, fit a distribution such that P(X > line) = consensus `fair_p_over`; return mean, median, p20, p80.

- **Yardage:** zero-inflated log-normal (or gamma; family configurable per market). Estimate the shape parameter per position/market from nflverse game logs 2021–2025 in a notebook under `notebooks/`, store it in a config file, and solve for location numerically.
- **Alternate lines present:** fit location and shape jointly by least squares across all lines.
- **Counts** (receptions, pass TDs, completions, INTs): Poisson by default; negative binomial where historical overdispersion is significant. Handle half-point lines exactly.
- **Anytime TD:** λ = −ln(1 − p_td).

Then replace the linear stub in `engine/consensus.py` with the fitted slope.

Validation: across 2023–2025 closing lines, the share of actual outcomes over the line should match `fair_p_over` within calibration buckets. Save a calibration plot per market to `reports/calibration/`.
