# Distribution shapes

Generated 2026-10-24 by `scripts/estimate_distribution_shapes.py` from nflverse weekly player stats (CC-BY 4.0), regular seasons 2021-2025. The shipped values are in `backend/config/distribution_params.json`.

`engine/distributions.py` turns the market's probability at one line into a full curve, which needs a **shape** the line cannot supply: how spread out a stat is for a given level. These are those shapes.

**Read these as upper bounds.** Each is the spread of a player's *own games within a season* (player-seasons with at least 8 qualifying games). Part of that spread is matchup, injury and weather, which the closing line already prices in, so the spread conditional on the line is smaller. After the backfill, `calibration-report` shows whether the fitted ranges cover about 60% of outcomes between p20 and p80, and how far to tighten.

## Yardage: gamma or log-normal

Zero-yard games are left out of the shape (they are modelled as a separate share), and each player-season gets its own average, so only the spread is pooled. The family with the higher log-likelihood is chosen.

| stat | pos | player-seasons | games | zero games | log-normal sigma | gamma shape k | log-lik gamma minus log-normal | family |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| pass_yds | QB | 162 | 2,234 | 0.0% | 0.317 | 10.84 | +63 | **gamma** |
| rush_yds | RB | 256 | 3,371 | 0.2% | 0.621 | 3.12 | +138 | **gamma** |
| rush_yds | QB | 81 | 953 | 3.4% | 0.806 | 2.11 | +78 | **gamma** |
| rec_yds | WR | 443 | 5,617 | 1.6% | 0.705 | 2.57 | +345 | **gamma** |
| rec_yds | TE | 167 | 2,024 | 0.8% | 0.668 | 2.79 | +108 | **gamma** |
| rec_yds | RB | 208 | 2,394 | 5.0% | 0.858 | 1.80 | +126 | **gamma** |

A smaller gamma shape means a wider spread (coefficient of variation = 1 / sqrt(k)).

## Counts: Poisson or negative binomial

The index of dispersion is variance / mean within player-seasons: 1 for a Poisson, above 1 for more spread than Poisson. A stat is negative binomial when the index is above 1.15 and a chi-square test rejects 1 at p < 0.01.

| stat | pos | player-seasons | mean | dispersion index | p (over-dispersed) | negative-binomial r | family |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| pass_tds | QB | 162 | 1.47 | 0.80 | 1 | — | **poisson** |
| pass_completions | QB | 162 | 21.21 | 1.40 | 1.9e-30 | 51.4 | **negbin** |
| interceptions | QB | 162 | 0.70 | 1.06 | 0.032 | 22.7 | **poisson** |
| rush_attempts | RB | 256 | 13.12 | 1.78 | 1.4e-140 | 16.5 | **negbin** |
| rush_attempts | QB | 162 | 3.84 | 1.24 | 7.3e-13 | 23.1 | **negbin** |
| receptions | WR | 455 | 4.21 | 0.95 | 1 | — | **poisson** |
| receptions | TE | 169 | 4.09 | 0.88 | 1 | — | **poisson** |
| receptions | RB | 219 | 3.16 | 0.88 | 1 | — | **poisson** |

Receptions and pass touchdowns come out *under*-dispersed (below 1), so Poisson is a little wider than the data. Anytime TD has no shape: the touchdown count is Poisson with lambda = -ln(1 - p).

## Zero-yard games by line

The share of games ending at zero by the size of the player's recent median (a stand-in for the posted line), smoothed so it never rises with the line. Low-volume players have low lines and often finish with nothing. These understate the true share: a player who was active but had no target or carry has no row in the game logs.

**rush_yds (RB)**

| typical line | zero share | games |
| ---: | ---: | ---: |
| 7.5 | 8.7% | 848 |
| 18.0 | 4.4% | 1,120 |
| 32.0 | 1.9% | 952 |
| 50.0 | 1.4% | 1,164 |
| 73.5 | 0.2% | 1,134 |

**rush_yds (QB)**

| typical line | zero share | games |
| ---: | ---: | ---: |
| 5.5 | 22.4% | 1,087 |
| 17.0 | 9.1% | 604 |
| 30.5 | 3.4% | 325 |
| 46.0 | 1.2% | 161 |

**rec_yds (WR)**

| typical line | zero share | games |
| ---: | ---: | ---: |
| 7.5 | 22.3% | 1,368 |
| 17.5 | 16.9% | 1,998 |
| 31.5 | 9.3% | 1,833 |
| 49.0 | 4.6% | 1,934 |
| 73.0 | 1.3% | 1,615 |

**rec_yds (TE)**

| typical line | zero share | games |
| ---: | ---: | ---: |
| 7.5 | 17.8% | 1,279 |
| 17.0 | 12.1% | 1,391 |
| 32.0 | 5.5% | 1,009 |
| 46.0 | 3.8% | 584 |
| 66.5 | 1.6% | 192 |

**rec_yds (RB)**

| typical line | zero share | games |
| ---: | ---: | ---: |
| 8.0 | 17.0% | 1,814 |
| 16.5 | 12.6% | 1,737 |
| 29.0 | 6.1% | 512 |

## Refreshing

```bash
cd backend && uv run python scripts/estimate_distribution_shapes.py
```
