# propline-fantasy

Market-implied NFL fantasy projections built from sportsbook player-prop lines, with line movement as an early signal.

Projections only — not betting advice.

- **Why and how:** [`docs/PLAN.md`](docs/PLAN.md)
- **Build order and progress:** [`docs/ROADMAP.md`](docs/ROADMAP.md)
- **Rules for working on the code:** [`CLAUDE.md`](CLAUDE.md)

## Layout

```
backend/     Python 3.12 · FastAPI · SQLAlchemy 2.0 + Alembic (managed with uv)
  app/         FastAPI app, routes, settings, logging
  engine/      pure projection library (no DB, HTTP or I/O)
  analytics/   movement, steam, context attribution
  ingest/      odds adapter, nflverse loaders, resolver, scheduler
  db/          models, sessions, migrations
  tests/
frontend/    Next.js (app router) · TypeScript · Tailwind
scripts/     local setup
docs/        plan and roadmap
```

## Requirements

- macOS with [Homebrew](https://brew.sh). Postgres, TimescaleDB and Redis run as Homebrew services; Docker is not needed.
- TimescaleDB comes from Timescale's own tap. Homebrew 6 won't load formulas from a third-party tap until you trust it, so run `brew trust timescale/tap` once before `make setup`.
- [uv](https://docs.astral.sh/uv/), which installs Python 3.12 for you
- Node.js 20.9 or newer

## Setup

```bash
make setup
```

This runs `scripts/setup-local.sh`, which:

1. Installs TimescaleDB (from the `timescale/tap` Homebrew tap), the Postgres version it is built for (currently `postgresql@18`), and Redis. TimescaleDB compiles from source, so it needs up-to-date Xcode Command Line Tools.
2. Configures Postgres to preload TimescaleDB and starts both services.
3. Creates the `propline` and `propline_test` databases with the TimescaleDB extension.
4. Copies `.env.example` to `.env` and `frontend/.env.example` to `frontend/.env.local` if they don't exist.

It then installs backend and frontend dependencies. It's safe to rerun.

Settings live in `.env`. The defaults match the parameter table in `CLAUDE.md`. `ODDS_API_KEY` can stay empty until the live ingestion steps.

## Running

```bash
make up
```

Starts Postgres and Redis, applies migrations, then runs:

- the backend at http://localhost:8000 (`/health` for liveness, `/health/ready` checks Postgres, TimescaleDB and Redis, `/docs` for the API docs)
- the frontend at http://localhost:3000

Ctrl-C stops the two servers; `make down` stops Postgres and Redis. If port 3000 is taken, use `make up FRONTEND_PORT=3001`.

## Other commands

| Command | What it does |
| --- | --- |
| `make test` | Backend tests (tests marked `live` are skipped) |
| `make lint` | ruff, ruff format check, mypy strict, frontend ESLint and TypeScript |
| `make format` | Auto-format backend code |
| `make migrate` | Apply database migrations |
| `make seed` | Load seed data (from Phase 1) |

CI on GitHub Actions runs the same lint, typecheck and test steps, with TimescaleDB and Redis as service containers.

## Data attribution

NFL data from [nflverse](https://github.com/nflverse) (CC-BY 4.0; FTN charting data CC-BY-SA 4.0). Odds data from [The Odds API](https://the-odds-api.com), subject to its terms.
