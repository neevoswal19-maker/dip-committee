# US stocks and ETFs: does the dip strategy work there?

Generated 2026-10-14 in 2 minutes from 518 S&P 500 / Nasdaq-100 and 30 ETF price histories. The live dip rules and exit plan, unchanged from India: $100,000, 8% positions, at most 15, entry at the next session's open. Costs are Groww's US schedule (0.1% brokerage, at least $1, plus 18% GST, and SEC/FINRA fees on sells) plus 0.5% for converting rupees on every order. SPY is held with dividends reinvested and no costs.

**Read with this caveat:** the stock lists are today's members. The S&P 500 adds companies after they have grown, so this is a list of past winners and every stock result is flattered, probably by more than in India. Tax is not deducted: gains held under 24 months are taxed at your slab rate, after that 12.5%, and the US withholds 25% of dividends.

## Pass criteria, fixed before running

In both 2014-21 and 2022 onward: growth above holding SPY, a worst fall no deeper than SPY's, and at least 50 closed trades (15 for ETFs).

## Decision: **no US stock signals**

No version beat holding SPY on both growth and worst fall in both periods.
ETFs: **fail**.

## Dip rules alone: fail

Why it failed:

- 2022+: only 48 closed trades (need 50)
- 2022+: grew 5.6% a year, not above SPY's 12.2%
- 2022+: worst fall -25.6%, deeper than SPY's -24.5%

| Period | Growth a year | Worst fall | SPY growth | SPY worst fall | Trades | Win rate | Invested |
|---|---|---|---|---|---|---|---|
| 2014-21 | 15.2% | -15.6% | 14.8% | -33.7% | 85 | 73% | 100% |
| 2022+ | 5.6% | -25.6% | 12.2% | -24.5% | 48 | 48% | 100% |

2022+ by market trend on the buy date: DOWNTREND 100% win, +27.6% avg (3), MIXED 50% win, +5.7% avg (8), UPTREND 43% win, -0.9% avg (37)

## Dip rules + down-day volume confirmation: fail

Why it failed:

- 2014-21: grew 12.2% a year, not above SPY's 14.8%
- 2022+: only 49 closed trades (need 50)
- 2022+: grew 7.2% a year, not above SPY's 12.2%

| Period | Growth a year | Worst fall | SPY growth | SPY worst fall | Trades | Win rate | Invested |
|---|---|---|---|---|---|---|---|
| 2014-21 | 12.2% | -26.5% | 14.8% | -33.7% | 87 | 68% | 100% |
| 2022+ | 7.2% | -23.7% | 12.2% | -24.5% | 49 | 51% | 100% |

2022+ by market trend on the buy date: DOWNTREND 100% win, +79.7% avg (1), MIXED 73% win, +14.5% avg (11), UPTREND 43% win, +0.5% avg (37)

## ETF dips: fail

Why it failed:

- 2014-21: grew 4.0% a year, not above SPY's 14.8%
- 2022+: only 11 closed trades (need 15)
- 2022+: grew -0.8% a year, not above SPY's 12.2%
- 2022+: worst fall -24.8%, deeper than SPY's -24.5%

| Period | Growth a year | Worst fall | SPY growth | SPY worst fall | Trades | Win rate | Invested |
|---|---|---|---|---|---|---|---|
| 2014-21 | 4.0% | -23.5% | 14.8% | -33.7% | 17 | 65% | 97% |
| 2022+ | -0.8% | -24.8% | 12.2% | -24.5% | 11 | 45% | 99% |

2022+ by market trend on the buy date: MIXED 100% win, +20.8% avg (4), UPTREND 14% win, -15.1% avg (7)

## In rupees, 2022 onward

What an Indian investor actually earns includes the rupee's move against the dollar (+27.5% since January 2022).

- SPY: 18.3% a year in rupees
- dip: 11.3% a year in rupees
- dip_volume: 13.0% a year in rupees

## For information: other exit plans on US stocks (dip rules alone)

**L1: 2.5xATR stop, trims at +25%/+50%, 20% trail after +30% (current alerts)**

| Period | Growth a year | Worst fall | SPY growth | SPY worst fall | Trades | Win rate | Invested |
|---|---|---|---|---|---|---|---|
| 2014-21 | 13.8% | -18.4% | 14.8% | -33.7% | 210 | 34% | 100% |
| 2022+ | 1.9% | -26.8% | 12.2% | -24.5% | 147 | 17% | 100% |

**L3: chandelier stop: highest high - 3xATR(22), no fixed targets**

| Period | Growth a year | Worst fall | SPY growth | SPY worst fall | Trades | Win rate | Invested |
|---|---|---|---|---|---|---|---|
| 2014-21 | 1.7% | -36.9% | 14.8% | -33.7% | 889 | 36% | 100% |
| 2022+ | -8.1% | -34.8% | 12.2% | -24.5% | 535 | 32% | 99% |

## For information: currency conversion cost (dip rules alone)

**0.0% per order**

| Period | Growth a year | Worst fall | SPY growth | SPY worst fall | Trades | Win rate | Invested |
|---|---|---|---|---|---|---|---|
| 2014-21 | 15.9% | -14.4% | 14.8% | -33.7% | 89 | 75% | 100% |
| 2022+ | 4.4% | -24.3% | 12.2% | -24.5% | 50 | 48% | 100% |

**1.0% per order**

| Period | Growth a year | Worst fall | SPY growth | SPY worst fall | Trades | Win rate | Invested |
|---|---|---|---|---|---|---|---|
| 2014-21 | 14.6% | -17.0% | 14.8% | -33.7% | 88 | 75% | 100% |
| 2022+ | 4.8% | -26.1% | 12.2% | -24.5% | 52 | 52% | 100% |

## Sources

- [Groww US stocks pricing](https://groww.in/pricing/us-stocks)
- [S&P 500 constituents](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies), [Nasdaq-100 constituents](https://en.wikipedia.org/wiki/Nasdaq-100)
