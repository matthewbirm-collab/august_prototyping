# Plan — Prop-Line Fantasy Projections

As of 2026-10-23. Source of truth for *why* and *how*; `docs/ROADMAP.md` is the *order*; `CLAUDE.md` holds the rules.

## 1. Concept

The app turns US sportsbook player-prop lines into market-implied fantasy projections, and treats line movement as the earliest signal that a projection should change.

**Thesis.** A prop line plus its over/under prices is the market's estimate of a player's stat distribution. Combining a player's passing, rushing, receiving, reception and TD props yields an expected fantasy score that already reflects injuries, weather, game script and sharp money — often hours before traditional projection sites update. Movement from open to current shows which way that information is pushing.

**Users.** Season-long managers (start/sit, waivers, trades) and DFS players who want a market-based baseline.

**v1 scope** — see `CLAUDE.md`.

## 2. Data sources

**Source preference (2026-10-23):** Patrick selected PropLine
(https://www.prop-line.com/docs), replacing the earlier ScoresAndOdds preference.
A free API key has been obtained and stored only in the Git-ignored local `.env`
as `PROPLINE_API_KEY`. The advertised free allowance is 1,000 requests/day.
Validate NFL market coverage and freshness against live responses before building
production ingestion. PropLine's NFL archive starts in 2026, so it does not satisfy
the planned 2023–2025 backtest; that source remains unresolved. The Odds API
information below describes the existing production adapter; PropLine currently
powers the isolated QB prototype, with production integration still pending. Keep provider access behind
`OddsProvider`; the app still computes its own fantasy projections.

| Need | Source | Notes |
| --- | --- | --- |
| Current player-prop lines, US books | The Odds API event-odds endpoint | One game per call; cost = markets × regions credits |
| Historical props for backtesting | The Odds API historical endpoints (paid) | Props back to May 2023 |
| Milestone props (e.g. 100+ rush yds) | `_alternate` market keys | Pins down distribution tails |
| Schedules, weekly rosters, injuries, depth charts, snap counts | nflreadpy | Polars, cached |
| Actual stats for grading | nflreadpy `load_player_stats()` | Ground truth |
| Player ID mapping | nflreadpy `load_ff_playerids()` | Bridges sportsbook names to canonical IDs |
| Baseline rankings | nflreadpy `load_ff_rankings()` | Backtest comparison |

**Credit budgeting.** 8 prop markets × 1 region = 8 credits per game per pull. A 16-game slate pulled hourly = 128 credits per sweep. Tier polling by time to kickoff and store every snapshot locally rather than re-buying history.

**Alternatives** if coverage/latency/history fall short: OddsPapi, SportsGameOdds, TheRundown, OpticOdds. Keep the provider behind an adapter.

**Licensing.** Most nflverse data is CC-BY 4.0; FTN charting is CC-BY-SA 4.0. Check each odds provider's terms on displaying or redistributing data before a public launch. If sportsbook links or affiliate deals are added, state advertising and responsible-gaming rules apply.

Sources: https://the-odds-api.com/sports/nfl-odds.html · https://pypi.org/project/nflreadpy/

## 3. Architecture

Every projection traces back to stored prop snapshots.

```mermaid
flowchart TD
    OA[The Odds API<br/>prop lines and prices] --> ING
    NV[nflverse via nflreadpy<br/>schedules, injuries, depth, stats] --> ING
    ING[Ingestion worker<br/>tiered polling, adapter, resolver, dedupe]
    ING -- writes snapshots --> DB[(Postgres + TimescaleDB<br/>snapshots, consensus, projections,<br/>movement, context)]
    ING -- triggers recompute --> ENG[Projection engine<br/>de-vig, consensus, fit, score<br/>movement, steam, context]
    DB <--> ENG
    DB -- reads --> API[FastAPI + Redis]
    ENG -- big moves --> AL[Alerts<br/>email, web push]
    API --> WEB[Next.js web app]
    WEB --> U[Fantasy managers]
    AL --> U
```

Keep `engine/` a pure library so the same code runs live and in backtests.

## 4. Projection methodology

1. **De-vig each book.** Over/under prices → implied probabilities → remove hold (multiplicative first; also power and Shin) → fair P(over) at that book's line. `engine/odds.py` implements this. For a two-way market Shin's method equals the additive one exactly, so the real choice is among multiplicative, additive/Shin and power. They agree on even markets and differ on lopsided ones: an anytime-TD line of Yes +1200 / No -3000 gives a touchdown probability of 7.4%, 5.5% or 4.0%. Multiplicative is the default until `/p3-distributions` calibration says otherwise.
2. **Consensus.** Combine books into one fair line and P(over), weighting sharper / fresher books more and dropping stale quotes. `engine/consensus.py` implements this: books on different lines are shifted to a common line (the weighted median of the lines posted) with a stand-in curve until `/p3-distributions` replaces it, and a book that posts only one side is de-vigged with an assumed margin (`ONE_SIDED_OVERROUND`, a placeholder to calibrate).
3. **Fit a distribution per stat.**
   - Yardage: skewed continuous (log-normal or gamma) with a point mass at zero for low-volume players, solved so P(X > line) = fair P(over). Alternate lines pin down shape. `engine/distributions.py` implements this. On 2021-2025 game logs gamma fit better than log-normal in every cell, so gamma is the default; shapes and the zero-yard share by line are in `backend/config/distribution_params.json`, with the evidence in `docs/DISTRIBUTION_SHAPES.md`. They are upper bounds until refitted against real closing lines.
   - Counts (receptions, pass TDs, completions, INTs): Poisson or negative binomial. Completions and rush attempts are over-dispersed (negative binomial); receptions and pass TDs are not (Poisson).
   - Anytime TD: λ = −ln(1 − P(TD)). Do not also add separate rush/rec TD props.
4. **Use means, not lines.** Lines sit near the median; yardage is right-skewed, so the mean is above the line.
5. **Score.** Apply the league's scoring weights to stat means. `engine/projection.py` does this: RB/WR/TE touchdowns come from anytime TD only; a QB's passing touchdowns come from the pass-TD prop and rushing scores from anytime TD; markets outside a position's stats (a receiver's rushing yards) are ignored. Confidence (0-1) is the product of four penalties, each weighted by the points a stat carries: missing markets, books disagreeing, fewer than three books, and old quotes. `ingest/projection_job.py` writes one `projections` row per player and scoring config whenever the result changes.
6. **Fill gaps.** No prop for a stat → team implied total (spread + total) × trailing usage share; flag low confidence. `engine/gapfill.py` does this: team plays, pass rate and offensive TDs move with implied points and the spread (slopes measured on 2021-2025 team-games); players get their per-game share of the last four games' targets, carries and pass attempts, with Out/Doubtful/reserve players zeroed and their share handed to teammates (a promoted depth-chart starter gets at least 75% of it); efficiency is pulled toward the position average. Filled stats are `source="model"` and count 0.4 of a market in confidence. Red-zone share is approximated by touchdown share blended with usage share.
7. **Range.** v1: per-stat p20/p80 assuming independence. v2: Monte Carlo with QB–pass-catcher correlation.

Standard scoring, r = 1 (PPR), 0.5 (half), 0 (standard):

```
E[FP] = 0.04·PassYds + 4·PassTD − 2·INT + 0.1·(RushYds + RecYds) + 6·TD + r·Rec − 2·FumLost
```

Fumbles lost have no market — use the player's historical rate. The rate is the player's last 17 games before `as_of`, blended with 6 games of the position's league rate (a starting value to replace with a measured one). All weights come from `scoring_configs`.

**Movement signals**
- Projection change since open and over 1h / 6h / 24h, attributed to the driving prop
- Book agreement: share of books moving the same direction
- Steam: several books moving within minutes
- Context join: moves matched to injury, depth-chart and weather updates by timestamp

**Validation.** Backtest 2023–2025 against actual box scores: MAE, within-position rank correlation, p20/p80 coverage; compare against `load_ff_rankings()`.

## 5. Risks

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Few/no props for backups and WR3s | Missing where start/sit is hardest | Gap filling, low-confidence labels |
| Retail books shade lines toward popular overs | Projections biased high | Weight sharper books; measure and correct bias in backtest |
| Provider terms restrict redistributing odds | Can't show raw lines | Show derived projections and fair probabilities; confirm terms early |
| Credit cost scales with markets × games × polls | Budget overruns | Tiered polling, dedupe, hard daily budget |
| Late scratches after the last poll | Stale projection at lock | 15-min polling inside 3h; inactive-list checks |
| Player name mismatches | Silent data loss | Resolver queue, unmatched-rate monitoring |

Open decisions are tracked at the bottom of `docs/ROADMAP.md`.
