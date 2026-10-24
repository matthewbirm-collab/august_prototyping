---
description: Phase 4 (optional) — grounded one-line movement explanations via the Claude API
---
Follow the working agreement in `CLAUDE.md`. Roadmap step: `/p4-summaries`.

Add an optional service that writes a one-sentence explanation for each top mover using the Claude API (Anthropic Python SDK; model name from config).

- Input to the model is structured JSON only: player, projection before/after, driver markets with deltas, matched context events.
- The system prompt must require using only the facts provided, never speculating about unreported injuries, and never giving betting advice.
- Validate output: length limit, and every number in the sentence must appear in the input. On failure, fall back to a template sentence.
- Cache by (player, computed_at). Feature-flag the whole service off by default.
- Build an eval set of 30 hand-labeled movers in `backend/tests/evals/` and report how many summaries pass review.
