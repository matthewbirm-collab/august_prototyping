# propline-fantasy

Market-implied NFL fantasy projections built from sportsbook player-prop lines, with line movement as an early signal.

Projections only — not betting advice.

- **Why and how:** [`docs/PLAN.md`](docs/PLAN.md)
- **Build order and progress:** [`docs/ROADMAP.md`](docs/ROADMAP.md)
- **Rules for working on the code:** [`CLAUDE.md`](CLAUDE.md)
- **Database schema:** [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md)
- **Fantasy research:** [Week 7 buy-low analysis (Weeks 3–6)](docs/research/2026-week-7/BUY_LOW.md)

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

Odds come from [The Odds API](https://the-odds-api.com) through the `OddsProvider` interface in `backend/ingest/odds/`. `DAILY_CREDIT_BUDGET` defaults to 15, which fits the free plan; the adapter refuses any call that could go over it. Tests use sample responses in `backend/tests/fixtures/odds_api/`, which are synthetic until real ones are recorded.

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
| `make test` | Backend tests (tests marked `live` are skipped). Database tests use `propline_test` and are skipped locally if Postgres isn't running |
| `cd backend && uv run python -m ingest.resolver_cli list` | Review sportsbook names the player resolver couldn't match; `confirm`, `ignore` and `rate --season --week` too |
| `cd backend && uv run ingest-nflverse --season 2026 [--week 8] [--datasets injuries,depth_charts] [--match-odds]` | Load nflverse schedules, players, rosters, injuries, depth charts, snap counts, box scores and FantasyPros rankings. Safe to rerun; cached for 6 hours in season |
| `cd backend && uv run run-ingest` | Capture live prop snapshots for every game that is due, update `consensus_snapshots` for the players whose quotes changed, then write `projections` (expected PPR / half-PPR / standard fantasy points, p20-p80 range, per-stat breakdown, confidence) for them. Stats with no prop (a backup's rushing yards, a WR3's receptions, most QB rushing) are filled from the game's spread and total and the player's last four games of usage, marked `model` and given lower confidence; rostered players with recent usage and no props at all are projected the same way. Needs `ODDS_API_KEY` |
| `cd backend && uv run run-ingest --dry-run` | Show this week's projected credit spend and which games are due. Makes no API calls and needs no key |
| `cd backend && uv run run-ingest [--tier auto\|15m\|1h\|3h\|12h] [--include-alternates] [--markets ...] [--bookmakers ...]` | Capture live prop snapshots for every game that is due, then exit (needs `ODDS_API_KEY`). `--serve` repeats it every 5 minutes with APScheduler |
| `cd backend && uv run backfill-props --seasons 2023,2024,2025` | Estimate the credit cost of backfilling historical props (paid Odds API plans). Loads the free nflverse data first and spends nothing |
| `cd backend && uv run backfill-props --seasons 2025 --weeks 8 --confirm` | Buy those snapshots (a pilot week). Resumable; `--probe --confirm` checks history works for 1 credit; `--report` shows status and unmatched rates; `--snapshots`, `--markets`, `--max-credits` and `--reserve` control the spend |
| `cd backend && uv run calibration-report --seasons 2023,2024,2025 --recompute` | After the historical backfill: compare the closing consensus with real stat lines. Writes per-market reliability tables, plots and `summary.md` to `reports/calibration/` and says whether the fitted ranges are too narrow or wide |
| `cd backend && uv run python scripts/estimate_distribution_shapes.py` | Re-estimate each stat's distribution shape from nflverse game logs (free). Rewrites `config/distribution_params.json` and `docs/DISTRIBUTION_SHAPES.md` |
| `cd backend && uv run pytest -m live` | Live checks against The Odds API; needs `ODDS_API_KEY` and only makes free calls |
| `make lint` | ruff, ruff format check, mypy strict, frontend ESLint and TypeScript |
| `make format` | Auto-format backend code |
| `make migrate` | Apply database migrations |
| `make seed` | Load reference data: 32 teams, US books, prop markets, PPR / half-PPR / standard scoring. Safe to rerun |

CI on GitHub Actions runs the same lint, typecheck and test steps, with TimescaleDB and Redis as service containers.

## Experimental quarterback model

An isolated research prototype compares quarterbacks using **four-point passing
TDs** and the scoring weights in `scoring_configs/qb_half_ppr.json`. This is not the
completed Phase 3 engine and does not change the web app.

With Python 3.12 available, add `PROPLINE_API_KEY` to the root `.env` (Git-ignored),
then run from the repository root:

```bash
python3 scripts/qb_model.py --fetch
```

This makes one bulk PropLine request and saves the snapshot plus Markdown and JSON
comparisons in `reports/qb-model/` (also Git-ignored). With the backend's uv setup,
the equivalent command is `cd backend && uv run python ../scripts/qb_model.py --fetch`.
To rerun offline without spending API quota:

```bash
python3 scripts/qb_model.py
```

Use `--reference "Brock Purdy"`, `--config PATH`, `--snapshot PATH`, `--output PATH`,
or `--as-of TIMESTAMP` to customize a run. The snapshot must have been captured at
or before `as_of`; replay cannot recover information from before that snapshot.
The default replay time is the snapshot's capture time, not today's date.

The report contains expected points, standard deviation, p10/p20/median/p80/p90,
probability of outscoring the reference, per-stat fits and contributions, exclusions,
and TD-margin sensitivity. It compares all sufficiently covered QBs; it does not
know which players are available in your fantasy league.

Method: combine fresh pregame quotes from DraftKings, FanDuel and Bovada; remove
the proportional margin from paired over/under prices; fit lognormal yardage
distributions from alternate thresholds and Poisson count distributions; then
simulate 20,000 independent outcomes per QB. Each book has equal total fit weight.
One-sided yardage ladders borrow the paired margin for that same player/stat/book.
Without paired TD odds, the configured 0.90 probability multiplier is an explicit
assumption, with 0.85 and 1.00 sensitivity runs. TD scorer prices are never
normalized across players. Provider player IDs merge book aliases; ambiguous
name-only mappings are excluded.

**Limitations:** not backtested or calibrated; conditional on normal workload;
independent stats/players; Poisson variance assumption; positive-only yardage;
no fumble losses, receiving points or bonuses. Missing/unfit markets exclude a QB
instead of silently contributing zero. Do not treat these intervals as validated
coverage guarantees or the small mean differences as decisive start/sit advice.
Historical fallback distributions, injury adjustments and correlations remain work
for the production engine.

The adapter reserves requests in a locked local daily quota ledger and reads server
usage headers. Other clients sharing the key can spend quota independently; the
provider enforces the shared limit. The script makes no automatic retries. Keys
are sent only in the authentication header and are not included in snapshots.

Run numerical and snapshot-boundary tests with `make test` after normal setup,
or without any third-party dependencies:

```bash
PYTHONPATH=backend python3 -m unittest discover -s backend/tests -p test_qb_model.py -v
```

## Data attribution

NFL data from [nflverse](https://github.com/nflverse) (CC-BY 4.0; FTN charting data CC-BY-SA 4.0). Odds data from [The Odds API](https://the-odds-api.com), subject to its terms.

The experimental QB model uses [PropLine](https://www.prop-line.com/docs) odds.
Provider selection does not grant redistribution rights; review its terms for any public use.

## Experimental wide receiver rankings

[Week 7 half-PPR rankings](docs/research/2026-week-7/wr-model/RANKINGS.md) use
Weeks 3–6 routes, target rates, YPRR and aDOT, with sample shrinkage, availability
flags and a frozen PropLine touchdown snapshot. The report includes simulated
ranges, sensitivity scenarios, explicit exclusions and a preliminary evaluation.
It is not the production projection engine or a live rankings service.

Run offline with Python 3.12: `python3 scripts/wr_model.py`. With uv:
`cd backend && uv run python ../scripts/wr_model.py`. Outputs go to
`reports/wr-model/`; no API calls are made. See the
[methodology](docs/research/2026-week-7/wr-model/MODEL.md) for coverage and limitations.
