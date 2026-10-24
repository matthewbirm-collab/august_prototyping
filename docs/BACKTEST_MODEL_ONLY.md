# Backtest: gap-fill model only (no markets)

How the projection does with **no prop lines at all**: every stat is filled from the game's
spread and total and each player's usage (`engine/gapfill.py`). This isn't the Phase 3 gate,
which is about market projections and needs the historical backfill. It does test the gap-fill
model, which is what backups and stats without props get.

```bash
uv run backtest --seasons 2023,2024,2025 --snapshots t24h,t1h,close --model-only
```

## How it was tuned

The knobs were tuned on **2023 only** (`scripts/tune_gapfill.py`); 2024 and 2025 were held out.
- Recent (4-game) shares blended with a 12-game window, by position.
- Shrinkage of yards per play.
- An opponent adjustment.
- A floor on the starting quarterback's share of passes.
- Correlations between a player's stats.
- A shared volume multiplier on filled stats, for the ranges.

One exception, said plainly: the tuner also picked a 0.7 long-window weight for quarterbacks. That
made 2024 and 2025 QB MAE 3.6% and 1.6% worse, so quarterbacks use 0. That choice was made after
seeing the held-out seasons, so **the QB rows below are not a clean hold-out**.

What didn't help, and was dropped:
- **Calibrating the filled means up to remove their 3-12% low bias** raised MAE, even on 2023.
  Fantasy points are right-skewed and MAE rewards the median, which sits below the mean.
- **Giving the depth-chart starter 96% of the passes** (winner-take-all) raised QB MAE. The chart
  names the wrong starter about one game in ten, and a wrong all-in guess costs a lot. A 70%
  floor helps instead.

## Held-out seasons, 2024-2025, close, PPR

MAE on the players both we and ECR project (head-to-head), before and after this change:

| position | before | after | ECR | vs ECR now |
| --- | ---: | ---: | ---: | --- |
| QB (not clean, see above) | 6.41 | 6.37 | 6.22 | 2.4% behind |
| RB | 4.65 | 4.58 | 4.72 | beats by 3.0% |
| WR | 4.83 | 4.74 | 4.76 | beats by 0.5% |
| TE | 3.86 | 3.79 | 3.73 | 1.6% behind |

Ranges (targets: 20% of outcomes below p20, 80% below p80):

| position | below p20 before → after | below p80 before → after |
| --- | --- | --- |
| QB | 23.7% → 16.4% | 68.0% → 73.2% |
| RB | 33.7% → 24.4% | 74.0% → 76.9% |
| WR | 34.4% → 20.3% | 72.9% → 76.4% |
| TE | 33.1% → 22.2% | 72.7% → 75.3% |

The p20-p80 band now holds about 55% of outcomes, against 38% before and a target of 60%.
The upper tail is still too thin: big games happen more often than the model allows.

## What's left
- **QB:** still about 1.2 points low on average, and behind ECR. Two-point conversions aren't
  projected (about 0.17 points a game for a QB). New and benched starters remain the largest
  errors.
- **TE:** 1.6% behind ECR.
- **Upper tail:** p80 coverage of 73-77%, not 80%. A heavier-tailed volume draw would help.
- **The gate itself** needs market projections, which need the historical backfill.

## All seasons, all snapshots (after)

| label | position | n | MAE | RMSE | bias | Spearman | below p20 | below p80 | model share | vs ECR (n) | our MAE | ECR MAE | beats ECR |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| close | QB | 1762 | 6.05 | 7.81 | -1.25 | 0.485 | 16.5% | 73.6% | 100% | 1346 | 6.20 | 6.10 | **no** |
| close | RB | 3953 | 4.53 | 6.24 | -0.20 | 0.678 | 23.9% | 76.7% | 100% | 3169 | 4.56 | 4.66 | yes |
| close | WR | 5974 | 4.80 | 6.57 | -0.40 | 0.587 | 19.7% | 76.0% | 100% | 4783 | 4.75 | 4.73 | **no** |
| close | TE | 2943 | 3.77 | 5.25 | -0.39 | 0.557 | 22.1% | 74.4% | 100% | 2362 | 3.76 | 3.72 | **no** |
| t1h | QB | 1762 | 6.05 | 7.81 | -1.25 | 0.485 | 16.5% | 73.6% | 100% | 1346 | 6.20 | 6.10 | **no** |
| t1h | RB | 3953 | 4.53 | 6.24 | -0.20 | 0.678 | 23.9% | 76.7% | 100% | 3169 | 4.56 | 4.66 | yes |
| t1h | WR | 5974 | 4.80 | 6.57 | -0.40 | 0.587 | 19.7% | 76.0% | 100% | 4783 | 4.75 | 4.73 | **no** |
| t1h | TE | 2943 | 3.77 | 5.25 | -0.39 | 0.557 | 22.1% | 74.4% | 100% | 2362 | 3.76 | 3.72 | **no** |
| t24h | QB | 1762 | 6.11 | 7.93 | -1.50 | 0.483 | 16.3% | 72.2% | 100% | 1308 | 6.24 | 6.11 | **no** |
| t24h | RB | 3953 | 4.50 | 6.28 | -0.47 | 0.674 | 22.8% | 75.3% | 100% | 3079 | 4.54 | 4.66 | yes |
| t24h | WR | 5974 | 4.77 | 6.58 | -0.65 | 0.588 | 18.7% | 74.5% | 100% | 4644 | 4.71 | 4.74 | yes |
| t24h | TE | 2943 | 3.76 | 5.28 | -0.59 | 0.558 | 21.3% | 73.2% | 100% | 2290 | 3.76 | 3.74 | **no** |

## Gate (close): beat ECR on MAE at every position

- QB: does not beat ECR
- RB: beats ECR
- WR: does not beat ECR
- TE: does not beat ECR
