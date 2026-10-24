---
description: Phase 5 — REST API (OpenAPI-first) and typed TypeScript client
---
Follow the working agreement in `CLAUDE.md`. Roadmap step: `/p5-api`.

Design the REST API OpenAPI-first, show me the spec, then implement it in FastAPI.

Endpoints:
- `GET /weeks/current`
- `GET /projections?season&week&position&scoring&sort&min_confidence`
- `GET /players/{id}` — profile, current projection, per-stat breakdown, injury/depth context
- `GET /players/{id}/history?market&book` — line and fair-probability series plus projection history
- `GET /movers?window=1h|6h|24h|open&position&limit`
- `POST /compare` — 2–4 player ids + scoring config
- `GET /scoring-configs`, `POST /scoring-configs`

Requirements: Redis caching keyed by latest `computed_at`, ETags, pagination, and an `as_of` timestamp on every response. Generate a typed TypeScript client from the spec into `frontend/lib/api/`.
