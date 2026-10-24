# Backtest: model/ppr

- Gap-fill model only (no markets), ppr scoring, seasons 2023,2024,2025, snapshots t24h, t1h, close.
- 43896 graded projections; 7120 more for players with no stat line (didn't play) and 0 players with nothing to project.
- ECR baseline: FantasyPros weekly ranks turned into points with a curve fitted on earlier seasons only, compared on the players both project.
- Game spreads and totals are nflverse's latest numbers, so earlier snapshots see the closing line.

| label | position | n | MAE | RMSE | bias | Spearman | below p20 | below p80 | model share | vs ECR (n) | our MAE | ECR MAE | beats ECR |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| close | QB | 1762 | 6.11 | 7.87 | -1.18 | 0.464 | 24.4% | 68.8% | 100% | 1346 | 6.22 | 6.10 | **no** |
| close | RB | 3953 | 4.61 | 6.35 | -0.06 | 0.665 | 33.8% | 74.0% | 100% | 3169 | 4.62 | 4.66 | yes |
| close | WR | 5974 | 4.89 | 6.67 | -0.25 | 0.573 | 33.9% | 72.0% | 100% | 4783 | 4.84 | 4.73 | **no** |
| close | TE | 2943 | 3.85 | 5.36 | -0.25 | 0.546 | 32.4% | 72.0% | 100% | 2362 | 3.84 | 3.72 | **no** |
| t1h | QB | 1762 | 6.11 | 7.87 | -1.18 | 0.464 | 24.4% | 68.8% | 100% | 1346 | 6.22 | 6.10 | **no** |
| t1h | RB | 3953 | 4.61 | 6.35 | -0.06 | 0.665 | 33.8% | 74.0% | 100% | 3169 | 4.62 | 4.66 | yes |
| t1h | WR | 5974 | 4.89 | 6.67 | -0.25 | 0.573 | 33.9% | 72.0% | 100% | 4783 | 4.84 | 4.73 | **no** |
| t1h | TE | 2943 | 3.85 | 5.36 | -0.25 | 0.546 | 32.4% | 72.0% | 100% | 2362 | 3.84 | 3.72 | **no** |
| t24h | QB | 1762 | 6.16 | 7.98 | -1.47 | 0.466 | 23.1% | 67.5% | 100% | 1308 | 6.26 | 6.11 | **no** |
| t24h | RB | 3953 | 4.59 | 6.37 | -0.28 | 0.661 | 32.6% | 72.9% | 100% | 3079 | 4.62 | 4.66 | yes |
| t24h | WR | 5974 | 4.86 | 6.67 | -0.46 | 0.575 | 33.0% | 70.9% | 100% | 4644 | 4.80 | 4.74 | **no** |
| t24h | TE | 2943 | 3.83 | 5.36 | -0.40 | 0.543 | 31.5% | 70.8% | 100% | 2290 | 3.83 | 3.74 | **no** |

## Gate (close): beat ECR on MAE at every position

- QB: does not beat ECR
- RB: beats ECR
- WR: does not beat ECR
- TE: does not beat ECR
