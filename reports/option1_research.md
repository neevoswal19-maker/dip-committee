# Option 1, backtested: the live BUY rule with delivery confirmation

Generated 2026-10-17 in 90 minutes. Today's Nifty 500, prices from Yahoo, delivery from NSE's daily bhavcopies (465 stocks, January 2020 to 2026-10-16). Live exit plan, Groww costs, entry at the next open, checked weekly, Rs 5 lakh capital. Benchmark: the Nifty 50 index (price only, so about 1.3% a year of dividends are not counted for it).

**What this cannot replay:** the quality gate (Yahoo keeps only about four years of statements) and the committee itself (past news and filings cannot be rebuilt). They are assumed to pass every candidate. Under option 1 the committee blocks only on a forensics veto or net-negative desks, so this is close to the live rule, but real results would differ by whatever those stages remove. Today's index members only, so survivorship flatters every figure, and tax is not deducted.

| Version | Period | Growth a year | Worst fall | Nifty 50 growth | Nifty 50 worst fall | Trades | Win rate | Invested |
|---|---|---|---|---|---|---|---|---|
| A: Dip rules alone, 8% x 15 | 2020-21 | 38.1% | -33.9% | 19.4% | -38.4% | 21 | 33% | 99% |
| A: Dip rules alone, 8% x 15 | 2022+ | 16.5% | -14.1% | 5.9% | -16.5% | 60 | 68% | 100% |
| A: Dip rules alone, 8% x 15 | 2020+ | 17.9% | -33.9% | 9.9% | -38.4% | 83 | 66% | 100% |
| B: Dip rules + delivery, 8% x 15 | 2020-21 | 29.2% | -32.5% | 19.4% | -38.4% | 27 | 48% | 95% |
| B: Dip rules + delivery, 8% x 15 | 2022+ | 14.3% | -15.6% | 5.9% | -16.5% | 56 | 68% | 100% |
| B: Dip rules + delivery, 8% x 15 | 2020+ | 15.2% | -32.5% | 9.9% | -38.4% | 93 | 66% | 99% |
| C: Option 1 as sized live: dip + delivery, 3% x 8 | 2020-21 | 6.1% | -9.3% | 19.4% | -38.4% | 17 | 41% | 95% |
| C: Option 1 as sized live: dip + delivery, 3% x 8 | 2022+ | 3.0% | -6.1% | 5.9% | -16.5% | 30 | 63% | 100% |
| C: Option 1 as sized live: dip + delivery, 3% x 8 | 2020+ | 4.4% | -9.3% | 9.9% | -38.4% | 53 | 70% | 99% |

Version C invests at most 8 x 3% = 24% of capital, because at conviction 50-59 each position risks 0.75% of capital to the -25% stop and total open risk is capped at 6%. The rest sits in cash, which this backtest earns nothing on.

## How many buy calls a year

Every delivery-confirmed dip signal taken as its own trade (one open trade per stock at a time), so this is how many distinct buy calls the rule makes, whatever room your portfolio has:

| Year | Buy calls (dip + delivery) | Dip signals without delivery |
|---|---|---|
| 2020 | 54 | 182 |
| 2021 | 166 | 365 |
| 2022 | 86 | 194 |
| 2023 | 106 | 194 |
| 2024 | 207 | 346 |
| 2025 | 103 | 188 |
| 2026 | 82 | 166 |

Across all 673 calls: 61% made money after costs, average 17.16% a trade, average win 43.6%, average loss -24.4%.
