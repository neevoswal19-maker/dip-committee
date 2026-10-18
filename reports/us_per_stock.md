# US stocks, one at a time: do dips in individual stocks pay?

Generated 2026-10-14 in 1 minutes from 518 price histories. Every dip signal from the live rules is taken as its own trade (one open trade per stock at a time), with the live exit plan (-25% stop, a quarter sold at +25% and +50%, 20% trail after +30%), $8,000 a trade, Groww's US costs and 0.5% currency conversion per order, entering at the next open. Each trade is compared with buying SPY (dividends reinvested) on the same entry day and selling it on the same exit day. **Dip minus SPY** is the number that matters: positive means buying the dip beat simply buying the index at that moment.

## The pre-registered test: the 30 Dow companies of January 2014

Chosen because nobody picked that list knowing what happened next. The rule, fixed before running: in both 2014-21 and 2022 onward, the average dip trade must beat SPY over the same dates after costs, with at least 30 trades in each.

### Result: fail

- 2014-21: dip trades averaged +9.4% against +13.1% for SPY over the same dates
- 2022+: dip trades averaged +13.8% against +23.4% for SPY over the same dates

| Group | Trades | Win rate | Avg trade | SPY same dates | Dip minus SPY | Beat SPY | Avg hold |
|---|---|---|---|---|---|---|---|
| 2014-21 | 97 | 68% | +9.4% | +13.1% | **-3.7%** | 51% | 18 months |
| 2022+ | 56 | 64% | +13.8% | +23.4% | **-9.5%** | 32% | 14 months |

Each Dow company, all years:

| Group | Trades | Win rate | Avg trade | SPY same dates | Dip minus SPY | Beat SPY | Avg hold |
|---|---|---|---|---|---|---|---|
| MSFT | 6 | 83% | +29.4% | +20.7% | **+8.7%** | 67% | 18 months |
| JPM | 5 | 80% | +11.7% | +4.8% | **+6.9%** | 60% | 13 months |
| TRV | 4 | 100% | +31.7% | +25.1% | **+6.6%** | 75% | 22 months |
| V | 4 | 100% | +25.4% | +19.1% | **+6.2%** | 50% | 18 months |
| PFE | 5 | 60% | +9.5% | +7.7% | **+1.9%** | 60% | 18 months |
| UNH | 6 | 50% | +9.9% | +8.4% | **+1.5%** | 50% | 13 months |
| CVX | 6 | 83% | +14.3% | +15.2% | **-0.8%** | 33% | 14 months |
| HD | 5 | 80% | +20.8% | +21.8% | **-1.0%** | 80% | 17 months |
| RTX | 6 | 67% | +8.4% | +9.6% | **-1.2%** | 50% | 15 months |
| GS | 6 | 50% | +9.9% | +11.1% | **-1.2%** | 50% | 15 months |
| JNJ | 4 | 100% | +35.0% | +38.2% | **-3.2%** | 50% | 23 months |
| INTC | 8 | 75% | +9.6% | +13.1% | **-3.5%** | 62% | 11 months |
| MRK | 5 | 80% | +15.0% | +19.2% | **-4.2%** | 60% | 17 months |
| WMT | 5 | 60% | +10.7% | +15.2% | **-4.6%** | 20% | 15 months |
| MCD | 6 | 67% | +12.8% | +17.4% | **-4.6%** | 50% | 18 months |
| AXP | 5 | 60% | +7.3% | +12.1% | **-4.9%** | 60% | 15 months |
| NKE | 5 | 80% | +14.3% | +21.3% | **-7.1%** | 60% | 17 months |
| IBM | 4 | 50% | +2.7% | +10.7% | **-7.9%** | 50% | 18 months |
| CAT | 6 | 50% | +1.7% | +10.0% | **-8.3%** | 33% | 13 months |
| GE | 6 | 33% | -2.6% | +5.8% | **-8.4%** | 33% | 11 months |
| PG | 3 | 67% | +10.0% | +19.4% | **-9.4%** | 33% | 23 months |
| XOM | 6 | 67% | +11.9% | +21.9% | **-10.0%** | 33% | 17 months |
| VZ | 5 | 80% | +10.9% | +22.2% | **-11.3%** | 40% | 23 months |
| KO | 5 | 100% | +31.7% | +44.1% | **-12.4%** | 20% | 24 months |
| CSCO | 3 | 67% | +7.5% | +20.0% | **-12.5%** | 33% | 16 months |
| DD | 5 | 40% | +1.2% | +14.1% | **-12.9%** | 20% | 16 months |
| MMM | 3 | 33% | -6.1% | +9.5% | **-15.6%** | 0% | 15 months |
| BA | 6 | 33% | -10.3% | +8.2% | **-18.5%** | 17% | 12 months |
| DIS | 5 | 60% | -3.8% | +16.7% | **-20.5%** | 20% | 17 months |
| T | 5 | 60% | +7.4% | +38.5% | **-31.1%** | 20% | 21 months |

## For information: all S&P 500 and Nasdaq-100 stocks

Today's members, so survivorship flatters these: companies that shrank out of the index are missing.

| Group | Trades | Win rate | Avg trade | SPY same dates | Dip minus SPY | Beat SPY | Avg hold |
|---|---|---|---|---|---|---|---|
| 2014-21 | 1675 | 68% | +12.8% | +12.1% | **+0.7%** | 50% | 15 months |
| 2022+ | 1086 | 63% | +11.7% | +19.0% | **-7.3%** | 36% | 12 months |

By sector, all years:

| Group | Trades | Win rate | Avg trade | SPY same dates | Dip minus SPY | Beat SPY | Avg hold |
|---|---|---|---|---|---|---|---|
| Technology | 35 | 66% | +19.2% | +12.3% | **+6.9%** | 57% | 9 months |
| Information Technology | 473 | 68% | +16.7% | +12.0% | **+4.7%** | 55% | 11 months |
| Energy | 134 | 63% | +10.2% | +11.5% | **-1.3%** | 40% | 11 months |
| Financials | 386 | 69% | +13.2% | +15.9% | **-2.7%** | 46% | 15 months |
| Consumer Discretionary | 293 | 62% | +9.7% | +13.0% | **-3.3%** | 47% | 12 months |
| Health Care | 328 | 64% | +12.2% | +15.6% | **-3.4%** | 44% | 14 months |
| Communication Services | 112 | 65% | +13.7% | +17.2% | **-3.5%** | 44% | 14 months |
| Industrials | 434 | 67% | +11.9% | +15.5% | **-3.7%** | 43% | 14 months |
| Materials | 134 | 63% | +7.9% | +11.8% | **-3.9%** | 40% | 13 months |
| Utilities | 143 | 74% | +15.6% | +19.6% | **-4.1%** | 38% | 17 months |
| Consumer Staples | 155 | 72% | +13.1% | +21.6% | **-8.5%** | 37% | 18 months |
| Real Estate | 134 | 51% | +1.8% | +14.0% | **-12.1%** | 29% | 16 months |

By US market trend on the buy date, all years:

| Group | Trades | Win rate | Avg trade | SPY same dates | Dip minus SPY | Beat SPY | Avg hold |
|---|---|---|---|---|---|---|---|
| DOWNTREND | 71 | 72% | +25.5% | +40.6% | **-15.1%** | 31% | 19 months |
| MIXED | 479 | 74% | +19.8% | +26.2% | **-6.4%** | 39% | 15 months |
| UPTREND | 2211 | 64% | +10.3% | +11.5% | **-1.2%** | 46% | 13 months |

Stocks with at least 3 dip trades: 497. Of these, 174 beat SPY on average.

Best 15 (hindsight: do not read these as picks):

| Group | Trades | Win rate | Avg trade | SPY same dates | Dip minus SPY | Beat SPY | Avg hold |
|---|---|---|---|---|---|---|---|
| NVDA | 9 | 89% | +75.4% | +12.4% | **+63.0%** | 89% | 10 months |
| PLTR | 3 | 100% | +74.0% | +13.0% | **+61.0%** | 100% | 7 months |
| MRNA | 5 | 80% | +65.2% | +7.9% | **+57.3%** | 80% | 3 months |
| HWM | 4 | 100% | +59.1% | +23.4% | **+35.7%** | 75% | 15 months |
| CIEN | 10 | 70% | +42.6% | +11.7% | **+31.0%** | 60% | 9 months |
| ANET | 7 | 86% | +39.7% | +12.0% | **+27.7%** | 71% | 11 months |
| VST | 5 | 40% | +37.3% | +12.4% | **+24.9%** | 40% | 11 months |
| BE | 4 | 50% | +37.1% | +12.8% | **+24.3%** | 50% | 3 months |
| MPC | 8 | 75% | +31.4% | +7.4% | **+24.1%** | 75% | 11 months |
| TKO | 7 | 86% | +36.2% | +13.0% | **+23.1%** | 57% | 13 months |
| STLD | 9 | 100% | +33.3% | +12.4% | **+20.9%** | 89% | 11 months |
| KLAC | 7 | 100% | +43.1% | +22.4% | **+20.7%** | 100% | 14 months |
| AAPL | 7 | 100% | +32.2% | +11.7% | **+20.5%** | 100% | 12 months |
| CRWD | 5 | 100% | +37.4% | +17.8% | **+19.6%** | 60% | 7 months |
| AXON | 8 | 75% | +36.0% | +17.1% | **+18.9%** | 62% | 9 months |

Worst 15:

| Group | Trades | Win rate | Avg trade | SPY same dates | Dip minus SPY | Beat SPY | Avg hold |
|---|---|---|---|---|---|---|---|
| GIS | 3 | 33% | -5.1% | +37.7% | **-42.8%** | 33% | 22 months |
| HSY | 5 | 60% | -0.1% | +35.2% | **-35.3%** | 0% | 20 months |
| KHC | 4 | 50% | -8.2% | +25.7% | **-33.9%** | 0% | 15 months |
| FIS | 3 | 33% | -4.2% | +27.9% | **-32.1%** | 33% | 16 months |
| CMCSA | 4 | 75% | +10.5% | +41.8% | **-31.2%** | 25% | 21 months |
| T | 5 | 60% | +7.4% | +38.5% | **-31.1%** | 20% | 21 months |
| PSA | 4 | 25% | -7.0% | +22.2% | **-29.1%** | 25% | 20 months |
| DOW | 4 | 25% | -14.6% | +13.4% | **-28.1%** | 0% | 8 months |
| FDX | 5 | 40% | -16.3% | +11.6% | **-27.9%** | 0% | 12 months |
| PTC | 5 | 20% | -10.5% | +17.4% | **-27.9%** | 0% | 15 months |
| FTV | 3 | 33% | -6.8% | +20.5% | **-27.3%** | 33% | 16 months |
| DVA | 6 | 17% | -8.7% | +18.2% | **-26.9%** | 0% | 10 months |
| HRL | 3 | 33% | +0.2% | +27.0% | **-26.8%** | 0% | 24 months |
| WYNN | 6 | 17% | -12.9% | +12.8% | **-25.6%** | 17% | 10 months |
| BIIB | 7 | 43% | -7.0% | +18.2% | **-25.2%** | 14% | 11 months |

## For interest only: today's largest companies

These are the biggest companies now *because* their shares rose, so dips in them paying off is partly true by construction. Never judged.

| Group | Trades | Win rate | Avg trade | SPY same dates | Dip minus SPY | Beat SPY | Avg hold |
|---|---|---|---|---|---|---|---|
| AAPL | 7 | 100% | +32.2% | +11.7% | **+20.5%** | 100% | 12 months |
| MSFT | 6 | 83% | +29.4% | +20.7% | **+8.7%** | 67% | 18 months |
| NVDA | 9 | 89% | +75.4% | +12.4% | **+63.0%** | 89% | 10 months |
| AMZN | 7 | 86% | +23.2% | +17.8% | **+5.5%** | 57% | 16 months |
| GOOGL | 7 | 86% | +36.3% | +24.5% | **+11.8%** | 71% | 17 months |
| META | 7 | 71% | +27.0% | +21.2% | **+5.8%** | 57% | 14 months |
| AVGO | 11 | 73% | +26.1% | +8.0% | **+18.1%** | 73% | 10 months |
| TSLA | 8 | 50% | +2.9% | +6.0% | **-3.1%** | 50% | 6 months |
| BRK.B | 4 | 75% | +11.7% | +24.7% | **-13.0%** | 0% | 22 months |
| JPM | 5 | 80% | +11.7% | +4.8% | **+6.9%** | 60% | 13 months |
