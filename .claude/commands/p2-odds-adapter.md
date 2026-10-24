---
description: Phase 2 — OddsProvider interface and The Odds API implementation
---
Follow the working agreement in `CLAUDE.md`. Plan: `docs/PLAN.md` §2. Roadmap step: `/p2-odds-adapter`.

Implement an `OddsProvider` interface in `backend/ingest/odds/` and a `TheOddsApiProvider` implementation.

Interface methods: `list_events(season, week)`, `list_event_markets(event_id)`, `get_event_props(event_id, markets, bookmakers)`, `get_historical_event_props(event_id, at, markets)`.

Details:
- Sport key `americanfootball_nfl`, region `us`, American odds.
- Fantasy markets: pass yds, pass TDs, pass completions, interceptions, rush yds, rush attempts, receptions, receiving yds, anytime TD, plus `_alternate` variants. **Confirm exact keys against the provider's market list before hard-coding; ask me if any are missing.**
- Parse responses into typed `PropQuote` records (event_id, book, market, player_name_raw, side, line, price, captured_at).
- Read the provider's remaining/used credit response headers and expose them; refuse any call that would exceed `DAILY_CREDIT_BUDGET`.
- Retries with exponential backoff on 429/5xx, timeouts, structured logs.
- Unit tests use recorded JSON fixtures in `backend/tests/fixtures/odds_api/`. One `@pytest.mark.live` integration test, skipped without `ODDS_API_KEY`.
