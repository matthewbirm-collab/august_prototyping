# The Odds API sample responses

**Synthetic.** These were written by hand from the v4 docs
(https://the-odds-api.com/liveapi/guides/v4/) before we had an API key, so field names and
shapes follow the documented examples, but players, prices and IDs are made up.

Once `ODDS_API_KEY` is set, replace them with real recorded responses (strip nothing but the
key, which never appears in a response body) and rerun `make test`.

| File | Endpoint |
| --- | --- |
| `events.json` | `GET /v4/sports/americanfootball_nfl/events` |
| `event_markets.json` | `GET /v4/sports/americanfootball_nfl/events/{eventId}/markets` |
| `event_odds.json` | `GET /v4/sports/americanfootball_nfl/events/{eventId}/odds` |
| `historical_event_odds.json` | `GET /v4/historical/sports/americanfootball_nfl/events/{eventId}/odds` |
| `historical_events.json` | `GET /v4/historical/sports/americanfootball_nfl/events` |
| `documented_nfl_prop_markets.json` | NFL player-prop market keys listed on the provider's betting-markets page |
