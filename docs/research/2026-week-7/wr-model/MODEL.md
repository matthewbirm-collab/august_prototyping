# Week 7 wide receiver model

This experimental half-PPR model ranks 93 receivers from a source universe of 193. It uses 582 receiver-game rows from Weeks 3–6 and 122 team-game rows. The frozen information cutoff is **2026-10-24 05:26:20 UTC** (October 23 at 11:26 p.m. Mountain time). It produces conditional forecasts for the remaining Week 7 games; Thursday's players are already locked.

[Rankings](RANKINGS.md) · [Results and per-player explanations](rankings.json) · [Weekly facts](weekly-inputs.csv) · [Team opportunities](team-dropbacks.csv) · [Availability and derived TD inputs](context.json)

## How the model works

This is an empirical-Bayes-style statistical prototype: observed rates are shrunk toward sample-derived priors. It is not a trained regression model or the production market-implied projection engine. Prior strengths and dispersion parameters are explicit assumptions in `scoring_configs/wr_half_ppr.json`.

1. **Team passing opportunities.** Average team dropbacks with weekly weights `0.8^(6 − week)`, then shrink toward the pooled team mean using two prior games. Only team games played enter the denominator. There is no defensive-matchup adjustment to passing volume.
2. **Route participation.** Blend 60% of the latest game's route share with 40% of the recency-weighted historical share. Shrink that blend toward 65% participation using 20 prior dropbacks. This gives role changes influence without treating a single game as definitive. Missing games are not invented zero-route games.
3. **Targets per route.** Shrink recency-weighted target rate toward the pooled WR target rate with 50 prior routes. Expected targets equal expected team dropbacks × shrunk route participation × shrunk target rate.
4. **Target depth and conversion.** Assign receivers to observed aDOT groups: below 7 yards, 7 to below 12, and at least 12. Each group's pooled catch rate and yards per catch supply priors. Shrink catch rate with 20 prior targets and yards per catch with 10 prior receptions. Group fallback uses the overall sample if a group has no catches. These priors describe this dataset, not a claimed league talent standard.
5. **Receptions and yards.** Expected catches = expected targets × shrunk catch rate. Expected receiving yards = catches × shrunk yards per catch. Observed YPRR remains visible, and shrunk YPRR equals target rate × catch rate × yards per catch. YPRR is not added as another independent score, which would double-count the same production. aDOT does not receive an arbitrary positive bonus.
6. **Total touchdowns.** Where available, use a same-event, fresh PropLine anytime-TD probability across equally weighted books, then `lambda = −ln(1 − p)`. Paired yes/no odds are proportionally de-vigged. One-sided probabilities use an assumed 0.90 multiplier, with 0.85 and 1.00 scenarios. Scorers are not mutually exclusive; probabilities are never normalized across players. Rushing and receiving TDs are not added again.
7. **Missing TD markets.** Four ranked players use a lower-confidence fallback: implied team points / 7 × 0.75 TD scoring fraction × 0.65 receiving fraction × observed target share. Those fractions are assumptions, not fitted facts. Team points come from the closest-to-even fresh full-game total/spread pair per book; alternate markets are not counted as independent books. This approximates a scoring mean from market lines and is an explicit limitation.
8. **Half-PPR scoring.** `0.5 × catches + 0.1 × receiving yards + 0.1 × rushing yards + 6 × total TDs`. Rushing yards are the sample mean shrunk toward zero with three prior games. Fumbles, two-point conversions, passing production and bonuses are omitted. All scoring coefficients are loaded from configuration.

The output is conditional on the modeled role, not necessarily a full-time starting role. Players changing teams use current-team usage only; a strong old-team role does not carry over automatically.

## Simulation and interpretation

Each player receives 10,000 reproducible draws. Team dropbacks follow a gamma–Poisson mixture with shape 25. Routes, targets and receptions are nested beta–binomial draws using the estimated rates. Receiving yards follow a gamma distribution conditional on receptions, with shape 2 per catch. This links yardage to catches rather than independently simulating unrelated stat totals. Total TDs follow a Poisson distribution with the estimated mean, independent of the receiving chain. Rushing yards are held at their projected mean.

The report includes analytical mean points, simulated standard deviation and p20–p80. These are **uncalibrated outcome ranges**, not confidence intervals or demonstrated coverage guarantees. They omit early exits, explicit DNP probabilities, uncertainty in yards-per-catch parameters and between-player correlations. The assumed dispersions can materially affect ranges.

A CHECK flag means a questionable/doubtful designation or nonparticipation in the captured practice report. These players retain a conditional ranking; the model does not invent a probability that they will play. The separate 75% workload scenario reduces routes and TD expectation together. It is illustrative, not an injury prediction. A player with no flag is not certified healthy.

The rank-sensitivity span compares six one-at-a-time scenarios: half/double efficiency-prior strengths, 0.85/1.00 TD multipliers, and 0%/100% latest-game role weight. It is not a joint uncertainty interval. Close scores and overlapping ranges should be treated as tiers rather than decisive ordering.

## Preliminary evaluation

The script runs three expanding-window checks. For each held-out week, priors and usage forecasts use only earlier weeks. Week 7 odds and injuries are not used. The target is receptions plus receiving-yardage points, with touchdowns and rushing omitted from both model and benchmark. The benchmark is the player's unweighted trailing mean over observed appearances.

| Held-out week | Player-games | Model MAE | Trailing-mean MAE |
| --- | ---: | ---: | ---: |
| 4 | 71 | 4.07 | 5.04 |
| 5 | 91 | 3.63 | 4.08 |
| 6 | 88 | 3.15 | 3.21 |
| Combined | 250 | **3.58** | **4.05** |

This preliminary result does not establish superiority over expert rankings, full-score accuracy or calibrated intervals. The sample selects players appearing in each held-out report and excludes prior sample/role gaps; it is not an unconditional forecast of all rostered players. The earliest fold has only one training week. The underlying pages were retrieved retrospectively and may contain stat corrections; this is not a strict publication-time historical backtest. The model parameters were not optimized against these results. The production roadmap's historical backtest gate remains open.

## Coverage and exclusions

The report names all exclusions: out, bye, already locked, insufficient sample, missing upcoming event, or absent from the latest team report. A player needs at least 20 routes and one target on his current team. This can omit a returning starter or rookie whose new role has not yet appeared in the data. Players with no Weeks 3–6 WR row are outside the universe entirely, even if they have a Week 7 prop market.

Notable limitations include limited injury-return evidence, no manual redistribution of vacated targets, no quarterback-specific efficiency adjustment, no fitted opponent/weather effect, and no long-term career prior. These omissions can matter more than a one-rank difference. The model should supplement a lineup decision, not automatically set a lineup.

## Sources and audit trail

Numeric weekly inputs were extracted from the **weekly** columns of Jerry Donabedian's RotoWire usage reports, which attribute the underlying statistics primarily to NFL Next Gen Stats. Cumulative percentage columns are not used. A duplicated preview table in Week 5 was excluded. No sportsbook websites were scraped.

- [Week 3](https://www.rotowire.com/football/article/nfl-box-score-breakdown-week-3-recap-usage-stats-137465)
- [Week 4](https://www.rotowire.com/football/article/nfl-box-score-breakdown-week-4-recap-usage-stats-139055)
- [Week 5](https://www.rotowire.com/football/article/nfl-box-score-breakdown-week-5-recap-usage-stats-140661)
- [Week 6](https://www.rotowire.com/football/article/nfl-box-score-breakdown-week-6-recap-usage-stats-142315)
- [Official Week 7 availability report](https://www.nfl.com/injuries/league/2026/REG7)
- [Week 7 schedule](https://www.nfl.com/schedules/2026/by-week/week-7)
- [Rams update supporting Adams's exclusion](https://www.therams.com/news/rams-injury-updates-cardinals-week-6-2026-blake-grupe-torn-acl-out-for-season-poona-ford-davante-adams-kobie-turner-trent-mcduffie-raiders)
- [PropLine](https://www.prop-line.com/): one bulk API request, using the existing quota-aware provider; derived probabilities and team expectations are frozen in `context.json`. Raw odds and credentials remain outside Git.

Earlier HeatRadar totals disagreed with the weekly reports. This model consistently uses the latter, so some figures differ from earlier conversational examples. Player IDs come from source profile URLs, not fuzzy name matching. Exact normalized names plus the scheduled event match TD inputs; unmatched names use the labeled fallback. No nflverse data were used for this particular snapshot.

[Source manifest](sources.json) records file hashes and provenance. Input CSVs and context are sufficient to reproduce the frozen rankings offline. They are research snapshots, not a live ingestion pipeline or a claim of permission to redistribute a commercial feed.

## Reproduce and review

From the repository root, with Python 3.12:

```sh
python3 scripts/wr_model.py
```

With the backend environment: `cd backend && uv run python ../scripts/wr_model.py`.

Default outputs go to `reports/wr-model/`. Use `--data`, `--config` and `--output` to supply another validated snapshot or scoring assumptions. `--as-of` must exactly match the frozen context timestamp: the tool refuses to relabel today's inputs as a historical or refreshed forecast. Recreating a different week requires replacing weekly facts, current rosters/availability, schedule and market context together. This command makes no API calls.

The published copy in this directory is a frozen run. Validation covers numeric consistency, source-sample cross-checks, no future weeks, duplicate rows, availability exclusions, score arithmetic, Monte Carlo mean agreement, reproducibility and regression protection for the QB adapter. Database and live-provider checks are separate.

Matt's review priorities: source definitions and data corrections; the 60% latest-role assumption; injury-return exclusions; depth-group shrinkage; and calibration on a larger historical sample before product integration.
