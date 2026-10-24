---
description: Phase 0 — scaffold the monorepo, local Homebrew services, CI and Makefile
---
Follow the working agreement in `CLAUDE.md`. Roadmap step: `/p0-scaffold`.

Scaffold a monorepo matching the target layout in `CLAUDE.md`:

- `backend/`: FastAPI app with a `/health` endpoint, settings via pydantic-settings (`.env`), structured JSON logging, SQLAlchemy 2.0 + Alembic wired up, uv for dependencies, ruff + mypy strict + pytest configured. Create empty `engine/`, `analytics/`, `ingest/`, `db/` packages with a short module docstring each.
- `frontend/`: Next.js app router, TypeScript, Tailwind, a placeholder home page that calls `/health`.
- Local services via Homebrew, not Docker (decided 2026-10-23): Postgres 17 with the TimescaleDB extension and Redis, run with `brew services`. Add `scripts/setup-local.sh` that installs and starts them and creates the dev and test databases; it must be safe to rerun. `make up` starts the Homebrew services, then the backend and frontend dev servers.
- CI uses GitHub Actions service containers (TimescaleDB image, Redis), so Docker is not needed locally.
- Remove the old Express starter (`index.js`, `src/`, `config/`, `tests/`, `package.json`, `package-lock.json`); it does not carry over. Git history keeps it.
- `.env.example` listing every setting (no real values), including `ODDS_API_KEY` and the default parameters table from `CLAUDE.md`.
- GitHub Actions CI: ruff, mypy, pytest, frontend lint and typecheck.
- `Makefile` targets: `up`, `down`, `test`, `lint`, `migrate`, `seed`.
- `README.md` with setup steps and an nflverse attribution section.

Propose the file tree first and wait for approval before generating files.

**Gate (Phase 0):** `make up` starts everything, `make test` passes, CI is green.
