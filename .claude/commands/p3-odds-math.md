---
description: Phase 3 — odds conversion and de-vig library
---
Follow the working agreement in `CLAUDE.md`. Plan: `docs/PLAN.md` §4 step 1. Roadmap step: `/p3-odds-math`.

Create `backend/engine/odds.py` with pure, fully tested functions:
- `american_to_decimal`, `decimal_to_american`, `american_to_implied_prob`
- `devig_two_way(p_over_raw, p_under_raw, method)` supporting `"multiplicative"`, `"additive"`, `"power"`, `"shin"`
- `hold(p_over_raw, p_under_raw)`

Property-based tests (hypothesis): de-vigged probabilities sum to 1, lie in (0, 1), and preserve ordering. Add docstrings explaining each method and its favorite–longshot bias. No I/O, no imports from `db/`, `app/` or `ingest/`.
