---
description: Phase 6 — metrics, data-quality checks and admin status page
---
Follow the working agreement in `CLAUDE.md`. Roadmap step: `/p6-observability`.

Add monitoring with OpenTelemetry and CloudWatch:
- **Metrics:** ingestion latency, quotes per run, unmatched-player rate, credits used vs budget, projection recompute time, API p95 latency.
- **Data-quality checks after every ingestion run:** games with no props within 24h of kickoff; a book missing for a whole slate; fair probabilities outside (0.02, 0.98); projections moving more than 50% with no market event.
- **Alerting** to Slack or email when a check fails or credits pass 80% of budget.
- **`/admin/status`** page summarizing the last 24 hours of runs.
