# CLAUDE.md — propline-fantasy

## Product
A fantasy football app that converts US sportsbook NFL player-prop lines into market-implied fantasy projections, and treats prop-line movement as the earliest signal that a projection should change.

Full plan: `docs/PLAN.md`. Build order and gates: `docs/ROADMAP.md`. Each build step is a slash command in `.claude/commands/` (`/p0-scaffold`, `/p1-data-model`, …). `/build-next` runs the next unchecked step.

### v1 scope
- Weekly market-implied projections for QB, RB, WR, TE in PPR, half-PPR and standard scoring
- Movement feed: biggest risers/fallers since open and over 1h / 6h / 24h, with the driver prop
- Player page: projection, p20–p80 range, per-prop breakdown, line history by book
- Start/sit comparison for 2–4 players
- Alerts on large projection moves for watched players

### Out of scope for v1
Kickers, team defenses, live in-game projections, betting picks, league sync (ESPN/Yahoo/Sleeper).

## Stack
- Backend: Python 3.12, FastAPI, Polars, SQLAlchemy 2.0 + Alembic, pydantic-settings
- Data: Postgres + TimescaleDB (prop snapshots as a hypertable), Redis cache
- Frontend: Next.js (app router, TypeScript, Tailwind)
- Infra: AWS — ECS Fargate (API + ingestion worker), RDS, ElastiCache, EventBridge Scheduler, Secrets Manager; CDK in TypeScript
- Tooling: uv, ruff, mypy (strict), pytest, hypothesis, GitHub Actions

## Repo layout (target)
```
backend/
  app/            FastAPI app, routes, schemas
  engine/         PURE projection library — no DB, no HTTP, no I/O
  analytics/      movement, steam, context attribution
  ingest/         odds adapter, nflverse loaders, resolver, scheduler
  db/             models, migrations
  tests/          unit (fixtures only) + @live integration
frontend/
infra/
docs/
```

## Data sources
- **The Odds API** — sport key `americanfootball_nfl`, region `us`. Player props come only from the per-event endpoint (`/v4/sports/{sport}/events/{eventId}/odds`), one game per call. Cost = markets × regions credits per call. Milestone lines use `_alternate` market keys. Historical props (paid plans) go back to May 2023.
- **nflverse via nflreadpy** — schedules, weekly rosters, injuries, depth charts, snap counts, player stats (ground truth), `load_ff_playerids()` for ID mapping, `load_ff_rankings()` for a baseline. Returns Polars DataFrames. Most data CC-BY 4.0; FTN charting CC-BY-SA 4.0 — attribute nflverse in README and app footer.
- All provider access goes through an `OddsProvider` interface so the provider can be swapped by config.

## Domain glossary
- **Prop**: a market on one player's stat (e.g. rush yards O/U 64.5).
- **Line**: the threshold (64.5). **Price**: American odds on each side (-115 / -105).
- **Implied probability**: probability from a price, including vig. **Vig/hold**: the book's margin; raw over+under probabilities sum to >1.
- **De-vig**: remove hold to get fair probabilities summing to 1.
- **Consensus**: weighted combination of books into one fair line and fair P(over).
- **Open / current / close**: first quote seen / latest / last before kickoff.
- **Steam**: several books moving the same market the same direction within minutes.
- **Alternate line**: extra thresholds for the same stat (e.g. 50+, 75+, 100+ yds).

## Projection rules (non-negotiable)
1. Fantasy points use stat **means**, not lines. Lines sit near the median; yardage is right-skewed.
2. For RB/WR/TE total TDs use **anytime TD** (λ = −ln(1 − p)). Never add separate rush + rec TD props on top.
3. All scoring weights come from `scoring_configs`. Never hard-code weights in engine code.
4. Stats with no market are gap-filled from team implied totals × usage share and marked `source="model"` with lower confidence.
5. The engine must be usable at any historical `as_of` timestamp with no data leakage.

## Conventions
- Typed Python everywhere; `mypy --strict` and `ruff` must pass.
- Unit tests never touch the network; use recorded fixtures in `backend/tests/fixtures/`. Live tests are marked `@pytest.mark.live` and skipped without keys.
- All timestamps UTC (`timestamptz`). Store American odds as integers; derive decimals/probabilities.
- Config and secrets via environment (`.env` locally, Secrets Manager in AWS). Never commit keys.
- Small, reviewable commits; one build step per branch/PR.

## Guardrails
- Never present output as betting advice or picks. UI copy says "projection", not "bet".
- Respect provider rate limits and the daily credit budget; the adapter must refuse calls that would exceed it.
- Do not scrape sportsbook websites.

## Default parameters (change here, not in code)
| Name | Default | Used by |
| --- | --- | --- |
| DAILY_CREDIT_BUDGET | 15 (The Odds API free plan, 500/month; ~5000 on a paid plan) | odds adapter |
| Polling tiers | >72h: 12h · 72–24h: 3h · 24–3h: 1h · <3h: 15m | scheduler |
| STALE_QUOTE_MINUTES | 90 | consensus |
| FUZZY_MATCH_THRESHOLD | 90 | player resolver |
| STEAM_MIN_BOOKS / STEAM_WINDOW_MIN | 3 / 10 | steam detection |
| STALE_LINE_PROB_PTS / STALE_LINE_MIN | 5 / 30 | stale-line detection |
| CONTEXT_LOOKBACK_HOURS | 24 | context attribution |
| ALERT_THRESHOLD_PTS | 2.0 | alerts |
| ALERT_DIGEST_MIN | 15 | alerts |

## Working agreement (applies to every build command)
1. Read this file, the referenced section of `docs/PLAN.md`, and the step's entry in `docs/ROADMAP.md`.
2. Inspect what already exists in the repo before proposing changes.
3. Propose a short plan (files to add/change, key decisions) and **wait for approval** before writing code.
4. If anything in angle brackets like `<THIS>` is still unresolved, ask me rather than guessing.
5. Implement with tests. Run `make test` (or the closest equivalent) and fix failures.
6. Finish by: updating README if behavior changed, checking the step off in `docs/ROADMAP.md`, and summarizing what changed and anything left open.

## Definition of done
Tests pass, types check, lint is clean, README updated, ROADMAP step checked, and the phase gate (if this step closes a phase) is demonstrated.
