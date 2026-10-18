# ETF swing systems: 2A metals, 2B sectors, 3 the portfolio

Generated 2026-10-17 in 1 minutes. Rules as proposed on 2026-10-17 (see src/strategy/etf_swing.py): trend and relative strength against the Nifty, a 3-7 day pullback to the 20-day EMA, entry at the next open, a 1.5 x ATR stop, and at +2R a trail below the 20-day EMA (2B sells 40% at +2R first). Portfolio: Rs 5 lakh, Rs 5,000 risked per position, at most Rs 1.4 lakh in one, at most 3, sector ETFs sold when the Nifty closes below its 50-day SMA. Groww delivery charges with ETF STT (0.001% on sells), plus 0.1% slippage each way.

**Not replayed:** the iNAV premium check, the 10:15-14:30 order window and the earnings blackout. Breadth uses today's five largest members of each sector. Tax is not deducted.

## Verdicts (long-history dataset, criteria fixed before running)

- **System 2A, metals: fail**. 2014-21: profit factor 0.33 below 1.3; 2014-21: average trade -1.33% after costs.
- **System 2B, sectors: fail**. 2014-21: profit factor 1.10 below 1.3.
- **System 3, the portfolio: fail**. 2014-21: grew -1.3% a year, not above NIFTYBEES's 14.4%; 2022+: grew 6.5% a year, not above NIFTYBEES's 6.8%.

## Long history

Assets: gold (GC=F*USDINR, from 2010-10-07, 144 signals), silver (SI=F*USDINR, from 2010-10-07, 89 signals), it (^CNXIT, from 2010-10-07, 36 signals), metal (^CNXMETAL, from 2010-10-07, 97 signals), auto (^CNXAUTO, from 2010-10-07, 96 signals), psu_bank (^CNXPSUBANK, from 2010-10-07, 63 signals), pharma (^CNXPHARMA, from 2011-01-31, 69 signals)

### System 2A, metals: every signal as its own trade

| Period | Trades | Win rate | Avg win | Avg loss | Profit factor | Avg trade | Avg hold |
|---|---|---|---|---|---|---|---|
| 2014-21 | 46 | 20% | 3.7% | -2.5% | 0.33 | -1.33% | 13 sessions |
| 2022+ | 21 | 43% | 14.3% | -3.8% | 3.33 | 3.96% | 14 sessions |

### System 2B, sectors: every signal as its own trade

| Period | Trades | Win rate | Avg win | Avg loss | Profit factor | Avg trade | Avg hold |
|---|---|---|---|---|---|---|---|
| 2014-21 | 100 | 40% | 6.4% | -3.9% | 1.10 | 0.21% | 14 sessions |
| 2022+ | 67 | 45% | 5.1% | -2.9% | 1.45 | 0.66% | 15 sessions |

### System 3, the portfolio

| Period | Growth a year | Worst fall | NIFTYBEES growth | NIFTYBEES worst fall | Trades | Win rate | Invested |
|---|---|---|---|---|---|---|---|
| 2014-21 | -1.3% | -19.2% | 14.4% | -36.3% | 139 | 32% | 59% |
| 2022+ | 6.5% | -13.4% | 6.8% | -15.7% | 79 | 44% | 57% |

### By asset, all years

| Asset | Trades | Win rate | Profit factor | Avg trade |
|---|---|---|---|---|
| gold | 30 | 33% | 1.07 | -0.05% |
| silver | 37 | 22% | 1.35 | 0.63% |
| it | 21 | 48% | 2.19 | 1.23% |
| metal | 44 | 36% | 1.01 | 0.23% |
| auto | 46 | 46% | 1.31 | 0.54% |
| psu_bank | 28 | 36% | 1.09 | -0.34% |
| pharma | 30 | 43% | 1.15 | 0.20% |

## Real etfs

Assets: gold (GOLDBEES.NS, from 2010-10-07, 71 signals), silver (SILVERBEES.NS, from 2022-02-04, 23 signals), it (ITBEES.NS, from 2020-06-30, 34 signals), metal (METALIETF.NS, from 2024-08-19, 9 signals), auto (AUTOBEES.NS, from 2022-01-24, 5 signals), psu_bank (PSUBNKBEES.NS, from 2010-10-07, 68 signals), pharma (PHARMABEES.NS, from 2021-07-07, 14 signals)

### System 2A, metals: every signal as its own trade

| Period | Trades | Win rate | Avg win | Avg loss | Profit factor | Avg trade | Avg hold |
|---|---|---|---|---|---|---|---|
| 2022+ | 28 | 32% | 14.8% | -3.8% | 2.01 | 2.18% | 16 sessions |

### System 2B, sectors: every signal as its own trade

| Period | Trades | Win rate | Avg win | Avg loss | Profit factor | Avg trade | Avg hold |
|---|---|---|---|---|---|---|---|
| 2022+ | 39 | 41% | 6.1% | -2.8% | 1.57 | 0.86% | 18 sessions |

### System 3, the portfolio

| Period | Growth a year | Worst fall | NIFTYBEES growth | NIFTYBEES worst fall | Trades | Win rate | Invested |
|---|---|---|---|---|---|---|---|
| 2022+ | 4.9% | -18.7% | 6.8% | -15.7% | 61 | 38% | 55% |

### By asset, all years

| Asset | Trades | Win rate | Profit factor | Avg trade |
|---|---|---|---|---|
| gold | 15 | 20% | 0.65 | -1.33% |
| silver | 13 | 46% | 4.18 | 6.23% |
| it | 12 | 33% | 0.56 | -0.59% |
| metal | 5 | 40% | 0.58 | -1.18% |
| auto | 4 | 50% | 1.81 | 1.23% |
| psu_bank | 10 | 50% | 4.33 | 3.38% |
| pharma | 8 | 38% | 1.42 | 0.96% |
