# Strategy research

Generated 2026-09-24 in 2 minutes. 49 Nifty 50 and 50 Nifty Next 50 price histories. Every figure is after Groww charges (brokerage, STT both sides, exchange, stamp duty, DP, GST) at Rs 50,000 per trade plus 0.05% slippage on market fills. Entry at the next session's open unless stated. A trade is a win only if it made money after costs.

**Read with this caveat:** both universes are today's index members, so stocks that dropped out are missing and every result is flattered. Profits would also be taxed as short-term capital gains (20%), which is not deducted here.

## Sanity check

RSI(2) on the Nifty 50 index itself, before costs, 2014 onward - published tests on broad indices find win rates well above 60%:

116 trades, win rate 65%, average trade 0.27%, profit factor 1.64.

## Swing strategies

Criteria, fixed before running: win rate at least 60% and profit factor at least 1.3 in both 2014-21 and 2022 onward; on the Nifty Next 50 at least 55% and above 1.1; and a 5-slot portfolio falling no further than simply holding the Nifty 50.

| Rule | Result | Stop / target (chosen on 2014-21) | Win 2014-21 | Win 2022+ | PF 2022+ | Avg trade 2022+ | Next 50 win | Portfolio CAGR / worst fall | Nifty 50 CAGR / worst fall |
|---|---|---|---|---|---|---|---|---|---|
| RSI(2) pullback | **fail** | no stop, target 1xATR, 10d | 68% | 68% | 1.04 | 0.04% | 69% | -8.2% / -34.6% | 6.2% / -16.5% |
| Cumulative RSI(2) | **fail** | no stop, rule exit, 10d | 63% | 60% | 0.92 | -0.09% | 63% | -12.2% / -46.9% | 6.2% / -16.5% |
| Double 7s | **fail** | no stop, rule exit, 10d | 63% | 61% | 1.09 | 0.13% | 62% | -1.9% / -32.3% | 6.2% / -16.5% |
| IBS reversal | **fail** | no stop, rule exit, 10d | 61% | 59% | 0.93 | -0.08% | 60% | -10.6% / -46.2% | 6.2% / -16.5% |
| TPS scale-in | **fail** | no stop, target 2xATR, 20d | 71% | 69% | 1.14 | 1.43% | 71% | -2.3% / -23.2% | 6.2% / -16.5% |
| Nifty 5-day losers | **fail** | no stop, target 2xATR, 20d | 64% | 59% | 1.02 | 0.05% | 64% | 3.8% / -23.3% | 6.2% / -16.5% |

### RSI(2) pullback - fail

*Entry:* close above the 200-day average and RSI(2) below 10. *Exit:* close above the 5-day average. *Source:* Connors & Alvarez, Short Term Trading Strategies That Work (2008).

Why it failed:

- 2014-21: profit factor 1.10 below 1.3
- 2022+: profit factor 1.04 below 1.3
- portfolio drawdown -34.6% worse than holding the Nifty 50 (-16.5%)

| Sample | Trades | Win rate | Avg win | Avg loss | Profit factor | Avg trade |
|---|---|---|---|---|---|---|
| 2014-21 (plan chosen here) | 2427 | 68% | 2.52% | -4.87% | 1.10 | 0.15% |
| 2022+ next-open | 1593 | 68% | 2.00% | -4.06% | 1.04 | 0.04% |
| 2022+ same-close | 1607 | 67% | 2.02% | -4.19% | 0.97 | -0.04% |
| Nifty Next 50, all years | 3623 | 69% | 2.84% | -5.10% | 1.27 | 0.40% |

All nine plans on 2014-21:

| Plan | Trades | Win rate | Profit factor | Avg trade |
|---|---|---|---|---|
| no stop, rule exit, 10d | 2611 | 62% | 1.06 | 0.07% |
| no stop, target 1xATR, 10d | 2427 | 68% | 1.10 | 0.15% |
| no stop, target 2xATR, 10d | 2217 | 56% | 1.23 | 0.44% |
| stop 2xATR, rule exit, 10d | 2765 | 62% | 0.98 | -0.03% |
| stop 2xATR, target 1xATR, 10d | 2632 | 65% | 0.96 | -0.06% |
| stop 2xATR, target 2xATR, 10d | 2417 | 53% | 1.08 | 0.17% |
| stop 3xATR, rule exit, 10d | 2658 | 62% | 1.01 | 0.01% |
| stop 3xATR, target 1xATR, 10d | 2483 | 67% | 0.98 | -0.03% |
| stop 3xATR, target 2xATR, 10d | 2271 | 55% | 1.10 | 0.21% |

Win rate by year: 2014 71% (390), 2015 63% (247), 2016 67% (249), 2017 71% (313), 2018 65% (254), 2019 67% (267), 2020 68% (240), 2021 69% (467), 2022 62% (298), 2023 68% (336), 2024 71% (451), 2025 71% (319), 2026 62% (189)

2022+ by market regime on the signal day: DOWNTREND 100% win, +1.56% avg (3), MIXED 66% win, -0.09% avg (570), UPTREND 68% win, +0.12% avg (1020)

2022+ by exit: target 1027 (+2.05% avg), time_stop 566 (-3.60% avg)

### Cumulative RSI(2) - fail

*Entry:* close above the 200-day average and the last two RSI(2) readings summing below 35. *Exit:* RSI(2) above 65. *Source:* Connors & Alvarez, Short Term Trading Strategies That Work (2008).

Why it failed:

- 2014-21: profit factor 1.10 below 1.3
- 2022+: win rate 60% below 60%
- 2022+: profit factor 0.92 below 1.3
- 2022+: loses -0.09% per trade on average
- portfolio drawdown -46.9% worse than holding the Nifty 50 (-16.5%)

| Sample | Trades | Win rate | Avg win | Avg loss | Profit factor | Avg trade |
|---|---|---|---|---|---|---|
| 2014-21 (plan chosen here) | 2678 | 63% | 2.10% | -3.27% | 1.10 | 0.12% |
| 2022+ next-open | 1688 | 60% | 1.74% | -2.80% | 0.92 | -0.09% |
| 2022+ same-close | 1693 | 61% | 1.57% | -3.02% | 0.82 | -0.22% |
| Nifty Next 50, all years | 3943 | 63% | 2.57% | -3.63% | 1.24 | 0.30% |

All nine plans on 2014-21:

| Plan | Trades | Win rate | Profit factor | Avg trade |
|---|---|---|---|---|
| no stop, rule exit, 10d | 2678 | 63% | 1.10 | 0.12% |
| no stop, target 1xATR, 10d | 2523 | 68% | 1.10 | 0.15% |
| no stop, target 2xATR, 10d | 2301 | 55% | 1.26 | 0.48% |
| stop 2xATR, rule exit, 10d | 2892 | 62% | 0.97 | -0.04% |
| stop 2xATR, target 1xATR, 10d | 2784 | 65% | 0.93 | -0.12% |
| stop 2xATR, target 2xATR, 10d | 2555 | 52% | 1.06 | 0.13% |
| stop 3xATR, rule exit, 10d | 2743 | 63% | 1.05 | 0.06% |
| stop 3xATR, target 1xATR, 10d | 2603 | 67% | 0.99 | -0.02% |
| stop 3xATR, target 2xATR, 10d | 2379 | 54% | 1.12 | 0.23% |

Win rate by year: 2014 64% (424), 2015 64% (274), 2016 61% (279), 2017 61% (357), 2018 66% (279), 2019 64% (283), 2020 64% (261), 2021 63% (521), 2022 53% (302), 2023 61% (364), 2024 65% (471), 2025 63% (354), 2026 48% (197)

2022+ by market regime on the signal day: DOWNTREND 86% win, +0.95% avg (7), MIXED 54% win, -0.33% avg (586), UPTREND 63% win, +0.03% avg (1095)

2022+ by exit: rule_exit 1583 (+0.38% avg), time_stop 105 (-7.23% avg)

### Double 7s - fail

*Entry:* close above the 200-day average and the lowest close of the last 7 days. *Exit:* the highest close of the last 7 days. *Source:* Connors & Alvarez, Short Term Trading Strategies That Work (2008).

Why it failed:

- 2014-21: profit factor 1.17 below 1.3
- 2022+: profit factor 1.09 below 1.3
- portfolio drawdown -32.3% worse than holding the Nifty 50 (-16.5%)

| Sample | Trades | Win rate | Avg win | Avg loss | Profit factor | Avg trade |
|---|---|---|---|---|---|---|
| 2014-21 (plan chosen here) | 3359 | 63% | 3.00% | -4.33% | 1.17 | 0.27% |
| 2022+ next-open | 2158 | 61% | 2.61% | -3.69% | 1.09 | 0.13% |
| 2022+ same-close | 2229 | 61% | 2.52% | -3.83% | 1.04 | 0.05% |
| Nifty Next 50, all years | 4962 | 62% | 3.47% | -4.68% | 1.22 | 0.38% |

All nine plans on 2014-21:

| Plan | Trades | Win rate | Profit factor | Avg trade |
|---|---|---|---|---|
| no stop, rule exit, 10d | 3359 | 63% | 1.17 | 0.27% |
| no stop, target 1xATR, 10d | 3556 | 68% | 1.11 | 0.16% |
| no stop, target 2xATR, 10d | 3119 | 56% | 1.25 | 0.47% |
| stop 2xATR, rule exit, 10d | 3686 | 60% | 1.04 | 0.07% |
| stop 2xATR, target 1xATR, 10d | 3914 | 64% | 0.93 | -0.11% |
| stop 2xATR, target 2xATR, 10d | 3432 | 53% | 1.09 | 0.17% |
| stop 3xATR, rule exit, 10d | 3455 | 62% | 1.09 | 0.15% |
| stop 3xATR, target 1xATR, 10d | 3665 | 67% | 0.98 | -0.03% |
| stop 3xATR, target 2xATR, 10d | 3216 | 55% | 1.14 | 0.28% |

Win rate by year: 2014 64% (510), 2015 63% (341), 2016 59% (361), 2017 66% (466), 2018 61% (368), 2019 62% (382), 2020 65% (327), 2021 61% (604), 2022 55% (400), 2023 64% (504), 2024 65% (590), 2025 60% (424), 2026 54% (240)

2022+ by market regime on the signal day: DOWNTREND 100% win, +1.04% avg (1), MIXED 57% win, -0.21% avg (725), UPTREND 63% win, +0.31% avg (1432)

2022+ by exit: rule_exit 1559 (+1.88% avg), time_stop 599 (-4.41% avg)

### IBS reversal - fail

*Entry:* close above the 200-day average, in the bottom 20% of the day's range, below the previous low. *Exit:* close in the top 20% of the day's range, or above the previous high. *Source:* Internal Bar Strength (Pagonidis); QuantifiedStrategies backtests.

Why it failed:

- 2014-21: profit factor 1.10 below 1.3
- 2022+: win rate 59% below 60%
- 2022+: profit factor 0.93 below 1.3
- 2022+: loses -0.08% per trade on average
- Nifty Next 50: profit factor 1.08 not above 1.1
- portfolio drawdown -46.2% worse than holding the Nifty 50 (-16.5%)

| Sample | Trades | Win rate | Avg win | Avg loss | Profit factor | Avg trade |
|---|---|---|---|---|---|---|
| 2014-21 (plan chosen here) | 4113 | 61% | 2.04% | -2.84% | 1.10 | 0.11% |
| 2022+ next-open | 2771 | 59% | 1.69% | -2.62% | 0.93 | -0.08% |
| 2022+ same-close | 2867 | 58% | 1.57% | -2.65% | 0.83 | -0.19% |
| Nifty Next 50, all years | 5970 | 60% | 2.43% | -3.39% | 1.08 | 0.10% |

All nine plans on 2014-21:

| Plan | Trades | Win rate | Profit factor | Avg trade |
|---|---|---|---|---|
| no stop, rule exit, 10d | 4113 | 61% | 1.10 | 0.11% |
| no stop, target 1xATR, 10d | 3604 | 67% | 1.09 | 0.13% |
| no stop, target 2xATR, 10d | 3137 | 55% | 1.22 | 0.41% |
| stop 2xATR, rule exit, 10d | 4242 | 60% | 0.96 | -0.06% |
| stop 2xATR, target 1xATR, 10d | 3859 | 64% | 0.92 | -0.13% |
| stop 2xATR, target 2xATR, 10d | 3349 | 52% | 1.08 | 0.15% |
| stop 3xATR, rule exit, 10d | 4151 | 61% | 1.04 | 0.04% |
| stop 3xATR, target 1xATR, 10d | 3682 | 66% | 0.98 | -0.03% |
| stop 3xATR, target 2xATR, 10d | 3197 | 55% | 1.12 | 0.24% |

Win rate by year: 2014 64% (619), 2015 56% (405), 2016 60% (462), 2017 62% (516), 2018 57% (433), 2019 61% (458), 2020 64% (419), 2021 59% (801), 2022 59% (532), 2023 64% (628), 2024 59% (725), 2025 55% (559), 2026 55% (327)

2022+ by market regime on the signal day: DOWNTREND 73% win, +0.29% avg (11), MIXED 56% win, -0.27% avg (923), UPTREND 60% win, +0.01% avg (1837)

2022+ by exit: rule_exit 2685 (+0.14% avg), time_stop 86 (-7.02% avg)

### TPS scale-in - fail

*Entry:* close above the 200-day average and RSI(2) below 25 two days running; adds 20/30/40% more on each lower close. *Exit:* RSI(2) above 70. *Source:* Connors, High Probability ETF Trading (2009).

Why it failed:

- 2014-21: profit factor 1.26 below 1.3
- 2022+: profit factor 1.14 below 1.3
- portfolio drawdown -23.2% worse than holding the Nifty 50 (-16.5%)

| Sample | Trades | Win rate | Avg win | Avg loss | Profit factor | Avg trade |
|---|---|---|---|---|---|---|
| 2014-21 (plan chosen here) | 1987 | 71% | 5.12% | -5.33% | 1.26 | 2.05% |
| 2022+ next-open | 1169 | 69% | 4.13% | -4.64% | 1.14 | 1.43% |
| 2022+ same-close | 1190 | 70% | 4.25% | -4.57% | 1.19 | 1.61% |
| Nifty Next 50, all years | 2706 | 71% | 5.97% | -5.83% | 1.32 | 2.53% |

All nine plans on 2014-21:

| Plan | Trades | Win rate | Profit factor | Avg trade |
|---|---|---|---|---|
| no stop, rule exit, 20d | 2740 | 78% | 1.01 | 0.99% |
| no stop, target 1xATR, 20d | 2377 | 81% | 1.17 | 1.27% |
| no stop, target 2xATR, 20d | 1987 | 71% | 1.26 | 2.05% |
| stop 2xATR, rule exit, 20d | 2987 | 69% | 0.85 | 0.52% |
| stop 2xATR, target 1xATR, 20d | 2795 | 69% | 0.89 | 0.55% |
| stop 2xATR, target 2xATR, 20d | 2389 | 57% | 1.00 | 1.09% |
| stop 3xATR, rule exit, 20d | 2818 | 75% | 0.97 | 0.83% |
| stop 3xATR, target 1xATR, 20d | 2525 | 77% | 1.01 | 0.94% |
| stop 3xATR, target 2xATR, 20d | 2118 | 65% | 1.13 | 1.60% |

Win rate by year: 2014 72% (319), 2015 72% (204), 2016 73% (224), 2017 77% (269), 2018 57% (198), 2019 72% (224), 2020 69% (194), 2021 70% (355), 2022 65% (231), 2023 74% (276), 2024 70% (326), 2025 75% (213), 2026 56% (123)

2022+ by market regime on the signal day: MIXED 67% win, +1.12% avg (371), UPTREND 71% win, +1.58% avg (798)

2022+ by exit: target 612 (+4.62% avg), time_stop 557 (-2.06% avg)

### Nifty 5-day losers - fail

*Entry:* weekly: the 5 Nifty 50 stocks above their 200-day average that fell most over 5 days. *Exit:* held 20 sessions. *Source:* Cross-sectional reversal on the Nifty 50 (delphicalpha, 2026).

Why it failed:

- 2022+: win rate 59% below 60%
- 2022+: profit factor 1.02 below 1.3
- portfolio drawdown -23.3% worse than holding the Nifty 50 (-16.5%)

| Sample | Trades | Win rate | Avg win | Avg loss | Profit factor | Avg trade |
|---|---|---|---|---|---|---|
| 2014-21 (plan chosen here) | 1359 | 64% | 5.06% | -6.52% | 1.38 | 0.88% |
| 2022+ next-open | 838 | 59% | 4.15% | -5.80% | 1.02 | 0.05% |
| 2022+ same-close | 849 | 59% | 4.19% | -5.69% | 1.08 | 0.18% |
| Nifty Next 50, all years | 2107 | 64% | 5.81% | -7.12% | 1.45 | 1.14% |

All nine plans on 2014-21:

| Plan | Trades | Win rate | Profit factor | Avg trade |
|---|---|---|---|---|
| no stop, rule exit, 20d | 1184 | 55% | 1.64 | 1.67% |
| no stop, target 1xATR, 20d | 1530 | 77% | 1.23 | 0.38% |
| no stop, target 2xATR, 20d | 1359 | 64% | 1.38 | 0.88% |
| stop 2xATR, rule exit, 20d | 1332 | 47% | 1.33 | 0.93% |
| stop 2xATR, target 1xATR, 20d | 1683 | 69% | 1.00 | 0.00% |
| stop 2xATR, target 2xATR, 20d | 1519 | 55% | 1.15 | 0.37% |
| stop 3xATR, rule exit, 20d | 1239 | 53% | 1.53 | 1.43% |
| stop 3xATR, target 1xATR, 20d | 1589 | 75% | 1.11 | 0.19% |
| stop 3xATR, target 2xATR, 20d | 1418 | 61% | 1.28 | 0.69% |

Win rate by year: 2014 70% (186), 2015 59% (164), 2016 69% (166), 2017 69% (184), 2018 58% (160), 2019 60% (172), 2020 62% (153), 2021 63% (174), 2022 53% (169), 2023 68% (189), 2024 56% (179), 2025 62% (183), 2026 51% (118)

2022+ by market regime on the signal day: MIXED 59% win, +0.28% avg (334), UPTREND 59% win, -0.10% avg (504)

2022+ by exit: target 406 (+4.63% avg), time_stop 432 (-4.25% avg)

## Long-term exit plans

The live dip rules on the Nifty 500 (15 positions of 8% of Rs 10 lakh, checked weekly, entry at the next open, costs included). Delivery confirmation and the committee are not applied - they cannot be reconstructed historically.

| Plan | CAGR 2014-21 | Worst fall 2014-21 | CAGR 2022+ | Worst fall 2022+ | Trades 2022+ | Win rate 2022+ |
|---|---|---|---|---|---|---|
| L1: 2.5xATR stop, trims at +25%/+50%, 20% trail after +30% (current alerts) | 18.5% | -20.4% | 12.3% | -20.9% | 152 | 24% |
| L2: -25% stop, trims at +25%/+50%, 20% trail after +30% | 16.0% | -18.3% | 14.6% | -13.8% | 57 | 63% |
| L3: chandelier stop: highest high - 3xATR(22), no fixed targets | 11.8% | -23.8% | 5.1% | -27.6% | 651 | 36% |

Holding the Nifty 50 from 2022: 6.2% a year, worst fall -16.5%.

**Decision: L2.** L2 beat the current plan on both growth and drawdown from 2022 onwards, on positions it was not chosen on.

## Sources

- Connors & Alvarez, *Short Term Trading Strategies That Work* (2008): RSI(2), cumulative RSI, Double 7s
- Connors, *High Probability ETF Trading* (2009): TPS
- [RSI(2) on 150 S&P 500 stocks, survivorship-free: 64.7% win, profit factor 1.01](https://www.elitetrader.com/et/threads/backtested-a-mean-reversion-rsi-2-pullback-strategy-on-150-s-p500-names.390710/)
- [Mean reversion on the Nifty 50](https://delphicalpha.substack.com/p/does-mean-reversion-work-on-the-nifty)
- [IBS strategies](https://www.quantifiedstrategies.com/ibs-internal-bar-strength-indicator-strategies/)
- [Double 7s](https://www.quantifiedstrategies.com/larry-connors-double-seven-strategy-does-it-still-work/)
- Chuck LeBeau, chandelier exit
