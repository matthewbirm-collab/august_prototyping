---
description: Phase 6 — AWS CDK infrastructure, staging and production
---
Follow the working agreement in `CLAUDE.md`. Roadmap step: `/p6-deploy`.

Write infrastructure as code in `infra/` with AWS CDK (TypeScript):
- VPC, RDS Postgres, ElastiCache Redis, ECS Fargate services for the API and the ingestion worker, EventBridge Scheduler rules matching the polling tiers, Secrets Manager for API keys, frontend on Amplify or Vercel.
- **TimescaleDB:** verify whether RDS supports the extension today. If not, propose Timescale Cloud or self-managed on EC2 and ask me before choosing.
- Separate staging and production stacks.
- GitHub Actions deploy workflow with manual approval for production.
- Estimate monthly cost at low traffic.

**Gate (Phase 6):** one live Sunday run end to end with no manual steps.
