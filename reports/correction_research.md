# Buying market corrections: backtest

Generated 2026-10-30 in 1 minutes. Episodes on the Nifty 500, 2005-09-26 to 2026-10-30; 453 stock histories (today's Nifty 500, dividend-adjusted). Rules in src/strategy/correction.py, fixed before running: buy every stock that fell further than the index from its pre-correction high, equal money each (Rs 5 lakh basket), at the next open after the trigger; sell at that old high, or after two years (E2 also at -25%). Groww delivery charges and slippage. The benchmark buys the index at the same open and sells when it regains its peak, or after two years, with no costs.

**Pass criteria:** at least 4 completed episodes; the basket beats the index trade in two-thirds of them and on average; at least 60% of trades reach their old high within two years. Episodes whose two years have not run out are shown but not judged.

**Read with this caveat:** today's index members only, so companies that never recovered and dropped out are missing, which flatters the target rate most of all. Returns are before tax.

**Survivorship check (a diagnostic, not a pass criterion):** 'All survivors' buys every stock in the universe with two years of history at the same open and sells when the index trade ends. How far it beats the index is roughly how much today's membership alone flatters any basket. The strategy's real edge is at most its own gap over the index minus that.

## Decision: **20% fall, Nifty 500, old high or 2 years, no stop**

| Trigger | Universe | Exit | Result | Avg return a year | Beat index by (avg pts) | All survivors beat it by | Why it failed |
|---|---|---|---|---|---|---|---|
| 10% | Nifty 100 | E1 | PASS | +29.6% | +7.8 | +5.0 | - |
| 10% | Nifty 100 | E2 | fail | +10.2% | -3.3 | +5.0 | beat the index in 4 of 12 episodes (need two-thirds); averaged -3.3 points against the index; 59% of trades reached their old high within two years (need 60%) |
| 10% | Nifty 500 | E1 | PASS | +36.2% | +11.2 | +6.4 | - |
| 10% | Nifty 500 | E2 | fail | +18.5% | -0.7 | +6.4 | beat the index in 5 of 12 episodes (need two-thirds); averaged -0.7 points against the index; 58% of trades reached their old high within two years (need 60%) |
| 15% | Nifty 100 | E1 | PASS | +35.2% | +13.9 | +9.1 | - |
| 15% | Nifty 100 | E2 | fail | +30.7% | +0.6 | +9.1 | beat the index in 4 of 7 episodes (need two-thirds) |
| 15% | Nifty 500 | E1 | PASS | +41.0% | +14.7 | +11.1 | - |
| 15% | Nifty 500 | E2 | fail | +35.0% | -3.4 | +11.1 | beat the index in 3 of 7 episodes (need two-thirds); averaged -3.4 points against the index; 58% of trades reached their old high within two years (need 60%) |
| 20% | Nifty 100 | E1 | PASS | +47.5% | +25.5 | +16.2 | - |
| 20% | Nifty 100 | E2 | fail | +28.9% | +2.7 | +16.2 | beat the index in 2 of 4 episodes (need two-thirds); 50% of trades reached their old high within two years (need 60%) |
| 20% | Nifty 500 | E1 | PASS | +49.4% | +23.9 | +23.4 | - |
| 20% | Nifty 500 | E2 | fail | +26.2% | -4.7 | +23.4 | beat the index in 1 of 4 episodes (need two-thirds); averaged -4.7 points against the index; 50% of trades reached their old high within two years (need 60%) |

### 10% fall, Nifty 100, old high or 2 years, no stop

| Triggered | Index fall | Stocks | Basket | Index trade | All survivors | Reached old high | Stopped | Avg months | Judged |
|---|---|---|---|---|---|---|---|---|---|
| 2005-10-20 | -10.4% | 48 | +51.6% | +12.1% | +10.3% | 75% | 0 | 9 | yes |
| 2006-05-19 | -13.7% | 38 | +20.2% | +15.1% | +19.8% | 89% | 0 | 10 | yes |
| 2007-02-28 | -11.1% | 48 | +6.0% | +12.5% | +10.0% | 77% | 0 | 8 | yes |
| 2007-08-17 | -10.7% | 49 | +17.2% | +11.6% | +8.7% | 96% | 0 | 5 | yes |
| 2008-01-18 | -10.5% | 56 | +3.2% | -14.2% | +18.3% | 59% | 0 | 19 | yes |
| 2015-06-11 | -10.6% | 54 | +17.8% | +12.2% | +19.9% | 69% | 0 | 14 | yes |
| 2016-11-21 | -11.5% | 56 | +21.1% | +12.9% | +14.7% | 89% | 0 | 8 | yes |
| 2018-03-23 | -10.9% | 52 | -9.6% | +13.2% | +11.1% | 54% | 0 | 16 | yes |
| 2018-10-04 | -11.4% | 47 | +12.2% | +14.2% | +15.0% | 72% | 0 | 10 | yes |
| 2020-03-06 | -10.5% | 63 | +36.1% | +14.8% | +21.3% | 97% | 0 | 9 | yes |
| 2022-02-24 | -13.3% | 68 | +21.6% | +13.8% | +25.5% | 85% | 0 | 11 | yes |
| 2023-03-14 | -10.3% | 55 | +24.8% | +10.7% | +13.9% | 95% | 0 | 5 | yes |
| 2024-11-13 | -10.5% | 73 | +12.2% | -0.1% | +13.2% | 70% | 0 | 13 | in progress |

### 10% fall, Nifty 100, old high or 2 years, -25% stop

| Triggered | Index fall | Stocks | Basket | Index trade | All survivors | Reached old high | Stopped | Avg months | Judged |
|---|---|---|---|---|---|---|---|---|---|
| 2005-10-20 | -10.4% | 48 | +45.3% | +12.1% | +10.3% | 73% | 6 | 8 | yes |
| 2006-05-19 | -13.7% | 38 | -12.8% | +15.1% | +19.8% | 26% | 28 | 3 | yes |
| 2007-02-28 | -11.1% | 48 | +11.2% | +12.5% | +10.0% | 73% | 13 | 4 | yes |
| 2007-08-17 | -10.7% | 49 | +11.5% | +11.6% | +8.7% | 86% | 7 | 2 | yes |
| 2008-01-18 | -10.5% | 56 | -23.4% | -14.2% | +18.3% | 5% | 53 | 2 | yes |
| 2015-06-11 | -10.6% | 54 | +3.2% | +12.2% | +19.9% | 52% | 20 | 8 | yes |
| 2016-11-21 | -11.5% | 56 | +21.0% | +12.9% | +14.7% | 88% | 4 | 7 | yes |
| 2018-03-23 | -10.9% | 52 | -5.0% | +13.2% | +11.1% | 42% | 30 | 9 | yes |
| 2018-10-04 | -11.4% | 47 | +12.6% | +14.2% | +15.0% | 72% | 13 | 6 | yes |
| 2020-03-06 | -10.5% | 63 | -15.4% | +14.8% | +21.3% | 11% | 56 | 1 | yes |
| 2022-02-24 | -13.3% | 68 | +16.8% | +13.8% | +25.5% | 81% | 10 | 8 | yes |
| 2023-03-14 | -10.3% | 55 | +24.3% | +10.7% | +13.9% | 95% | 1 | 5 | yes |
| 2024-11-13 | -10.5% | 73 | +8.7% | -0.1% | +13.2% | 63% | 16 | 11 | in progress |

### 10% fall, Nifty 500, old high or 2 years, no stop

| Triggered | Index fall | Stocks | Basket | Index trade | All survivors | Reached old high | Stopped | Avg months | Judged |
|---|---|---|---|---|---|---|---|---|---|
| 2005-10-20 | -10.4% | 146 | +87.9% | +12.1% | +8.4% | 77% | 0 | 9 | yes |
| 2006-05-19 | -13.7% | 129 | +18.1% | +15.1% | +17.3% | 88% | 0 | 10 | yes |
| 2007-02-28 | -11.1% | 151 | +23.8% | +12.5% | +8.3% | 87% | 0 | 7 | yes |
| 2007-08-17 | -10.7% | 147 | +16.0% | +11.6% | +11.5% | 92% | 0 | 5 | yes |
| 2008-01-18 | -10.5% | 171 | -2.1% | -14.2% | +52.6% | 46% | 0 | 20 | yes |
| 2015-06-11 | -10.6% | 227 | +22.9% | +12.2% | +26.0% | 79% | 0 | 11 | yes |
| 2016-11-21 | -11.5% | 243 | +23.2% | +12.9% | +16.4% | 93% | 0 | 6 | yes |
| 2018-03-23 | -10.9% | 246 | -13.0% | +13.2% | +7.0% | 46% | 0 | 16 | yes |
| 2018-10-04 | -11.4% | 269 | +2.3% | +14.2% | +7.0% | 61% | 0 | 14 | yes |
| 2020-03-06 | -10.5% | 270 | +38.4% | +14.8% | +15.0% | 93% | 0 | 10 | yes |
| 2022-02-24 | -13.3% | 328 | +24.3% | +13.8% | +22.5% | 86% | 0 | 11 | yes |
| 2023-03-14 | -10.3% | 277 | +22.1% | +10.7% | +13.8% | 94% | 0 | 6 | yes |
| 2024-11-13 | -10.5% | 337 | +5.9% | -0.1% | +8.2% | 62% | 0 | 14 | in progress |

### 10% fall, Nifty 500, old high or 2 years, -25% stop

| Triggered | Index fall | Stocks | Basket | Index trade | All survivors | Reached old high | Stopped | Avg months | Judged |
|---|---|---|---|---|---|---|---|---|---|
| 2005-10-20 | -10.4% | 146 | +76.4% | +12.1% | +8.4% | 70% | 24 | 7 | yes |
| 2006-05-19 | -13.7% | 129 | -15.2% | +15.1% | +17.3% | 22% | 101 | 2 | yes |
| 2007-02-28 | -11.1% | 151 | +25.3% | +12.5% | +8.3% | 81% | 28 | 4 | yes |
| 2007-08-17 | -10.7% | 147 | +10.5% | +11.6% | +11.5% | 82% | 26 | 2 | yes |
| 2008-01-18 | -10.5% | 171 | -25.0% | -14.2% | +52.6% | 3% | 166 | 1 | yes |
| 2015-06-11 | -10.6% | 227 | +11.1% | +12.2% | +26.0% | 65% | 68 | 7 | yes |
| 2016-11-21 | -11.5% | 243 | +22.7% | +12.9% | +16.4% | 92% | 16 | 5 | yes |
| 2018-03-23 | -10.9% | 246 | -4.5% | +13.2% | +7.0% | 38% | 153 | 7 | yes |
| 2018-10-04 | -11.4% | 269 | +0.4% | +14.2% | +7.0% | 52% | 126 | 7 | yes |
| 2020-03-06 | -10.5% | 270 | -18.9% | +14.8% | +15.0% | 9% | 246 | 1 | yes |
| 2022-02-24 | -13.3% | 328 | +15.7% | +13.8% | +22.5% | 72% | 77 | 8 | yes |
| 2023-03-14 | -10.3% | 277 | +21.4% | +10.7% | +13.8% | 92% | 13 | 5 | yes |
| 2024-11-13 | -10.5% | 337 | +2.3% | -0.1% | +8.2% | 49% | 148 | 8 | in progress |

### 15% fall, Nifty 100, old high or 2 years, no stop

| Triggered | Index fall | Stocks | Basket | Index trade | All survivors | Reached old high | Stopped | Avg months | Judged |
|---|---|---|---|---|---|---|---|---|---|
| 2006-05-22 | -18.6% | 37 | +28.4% | +23.1% | +27.1% | 86% | 0 | 10 | yes |
| 2007-03-05 | -15.3% | 48 | +11.3% | +16.5% | +15.0% | 77% | 0 | 8 | yes |
| 2008-01-21 | -19.6% | 47 | +22.3% | +2.7% | +38.0% | 60% | 0 | 20 | yes |
| 2016-01-18 | -16.3% | 52 | +51.7% | +19.3% | +29.2% | 77% | 0 | 15 | yes |
| 2018-10-23 | -15.0% | 47 | +13.7% | +17.0% | +16.3% | 68% | 0 | 11 | yes |
| 2020-03-12 | -21.9% | 57 | +72.2% | +37.1% | +46.9% | 96% | 0 | 10 | yes |
| 2022-05-12 | -15.2% | 67 | +30.3% | +17.1% | +23.8% | 85% | 0 | 12 | yes |
| 2025-02-14 | -15.8% | 67 | +23.6% | +6.8% | +23.4% | 63% | 0 | 12 | in progress |

### 15% fall, Nifty 100, old high or 2 years, -25% stop

| Triggered | Index fall | Stocks | Basket | Index trade | All survivors | Reached old high | Stopped | Avg months | Judged |
|---|---|---|---|---|---|---|---|---|---|
| 2006-05-22 | -18.6% | 37 | +8.8% | +23.1% | +27.1% | 54% | 16 | 6 | yes |
| 2007-03-05 | -15.3% | 48 | +17.8% | +16.5% | +15.0% | 77% | 11 | 5 | yes |
| 2008-01-21 | -19.6% | 47 | -16.7% | +2.7% | +38.0% | 15% | 40 | 6 | yes |
| 2016-01-18 | -16.3% | 52 | +50.6% | +19.3% | +29.2% | 77% | 4 | 14 | yes |
| 2018-10-23 | -15.0% | 47 | +11.3% | +17.0% | +16.3% | 68% | 15 | 6 | yes |
| 2020-03-12 | -21.9% | 57 | +40.4% | +37.1% | +46.9% | 60% | 21 | 7 | yes |
| 2022-05-12 | -15.2% | 67 | +24.5% | +17.1% | +23.8% | 78% | 8 | 10 | yes |
| 2025-02-14 | -15.8% | 67 | +22.6% | +6.8% | +23.4% | 63% | 8 | 11 | in progress |

### 15% fall, Nifty 500, old high or 2 years, no stop

| Triggered | Index fall | Stocks | Basket | Index trade | All survivors | Reached old high | Stopped | Avg months | Judged |
|---|---|---|---|---|---|---|---|---|---|
| 2006-05-22 | -18.6% | 128 | +28.9% | +23.1% | +25.6% | 88% | 0 | 11 | yes |
| 2007-03-05 | -15.3% | 153 | +30.6% | +16.5% | +13.8% | 88% | 0 | 7 | yes |
| 2008-01-21 | -19.6% | 158 | +18.9% | +2.7% | +72.8% | 47% | 0 | 20 | yes |
| 2016-01-18 | -16.3% | 227 | +41.8% | +19.3% | +24.2% | 84% | 0 | 13 | yes |
| 2018-10-23 | -15.0% | 241 | +6.9% | +17.0% | +8.9% | 57% | 0 | 14 | yes |
| 2020-03-12 | -21.9% | 255 | +76.2% | +37.1% | +42.3% | 92% | 0 | 10 | yes |
| 2022-05-12 | -15.2% | 321 | +32.5% | +17.1% | +22.8% | 85% | 0 | 12 | yes |
| 2025-02-14 | -15.8% | 362 | +19.1% | +6.8% | +21.8% | 54% | 0 | 13 | in progress |

### 15% fall, Nifty 500, old high or 2 years, -25% stop

| Triggered | Index fall | Stocks | Basket | Index trade | All survivors | Reached old high | Stopped | Avg months | Judged |
|---|---|---|---|---|---|---|---|---|---|
| 2006-05-22 | -18.6% | 128 | -3.0% | +23.1% | +25.6% | 38% | 79 | 4 | yes |
| 2007-03-05 | -15.3% | 153 | +32.7% | +16.5% | +13.8% | 84% | 24 | 4 | yes |
| 2008-01-21 | -19.6% | 158 | -18.4% | +2.7% | +72.8% | 11% | 140 | 4 | yes |
| 2016-01-18 | -16.3% | 227 | +32.3% | +19.3% | +24.2% | 74% | 42 | 10 | yes |
| 2018-10-23 | -15.0% | 241 | +3.5% | +17.0% | +8.9% | 51% | 115 | 7 | yes |
| 2020-03-12 | -21.9% | 255 | +35.4% | +37.1% | +42.3% | 54% | 111 | 5 | yes |
| 2022-05-12 | -15.2% | 321 | +26.0% | +17.1% | +22.8% | 75% | 51 | 10 | yes |
| 2025-02-14 | -15.8% | 362 | +16.7% | +6.8% | +21.8% | 52% | 85 | 11 | in progress |

### 20% fall, Nifty 100, old high or 2 years, no stop

| Triggered | Index fall | Stocks | Basket | Index trade | All survivors | Reached old high | Stopped | Avg months | Judged |
|---|---|---|---|---|---|---|---|---|---|
| 2006-06-01 | -20.6% | 37 | +29.4% | +25.9% | +29.3% | 86% | 0 | 10 | yes |
| 2008-01-22 | -24.9% | 48 | +18.9% | -6.6% | +35.2% | 58% | 0 | 20 | yes |
| 2016-02-11 | -20.5% | 47 | +63.4% | +25.3% | +35.1% | 74% | 0 | 15 | yes |
| 2020-03-12 | -21.9% | 57 | +72.2% | +37.1% | +46.9% | 96% | 0 | 10 | yes |

### 20% fall, Nifty 100, old high or 2 years, -25% stop

| Triggered | Index fall | Stocks | Basket | Index trade | All survivors | Reached old high | Stopped | Avg months | Judged |
|---|---|---|---|---|---|---|---|---|---|
| 2006-06-01 | -20.6% | 37 | +7.3% | +25.9% | +29.3% | 54% | 17 | 6 | yes |
| 2008-01-22 | -24.9% | 48 | -18.7% | -6.6% | +35.2% | 12% | 42 | 5 | yes |
| 2016-02-11 | -20.5% | 47 | +63.4% | +25.3% | +35.1% | 74% | 3 | 14 | yes |
| 2020-03-12 | -21.9% | 57 | +40.4% | +37.1% | +46.9% | 60% | 21 | 7 | yes |

### 20% fall, Nifty 500, old high or 2 years, no stop

| Triggered | Index fall | Stocks | Basket | Index trade | All survivors | Reached old high | Stopped | Avg months | Judged |
|---|---|---|---|---|---|---|---|---|---|
| 2006-06-01 | -20.6% | 122 | +30.3% | +25.9% | +27.9% | 86% | 0 | 10 | yes |
| 2008-01-22 | -24.9% | 157 | +16.6% | -6.6% | +72.5% | 46% | 0 | 21 | yes |
| 2016-02-11 | -20.5% | 220 | +54.1% | +25.3% | +32.3% | 84% | 0 | 12 | yes |
| 2020-03-12 | -21.9% | 255 | +76.2% | +37.1% | +42.3% | 92% | 0 | 10 | yes |

### 20% fall, Nifty 500, old high or 2 years, -25% stop

| Triggered | Index fall | Stocks | Basket | Index trade | All survivors | Reached old high | Stopped | Avg months | Judged |
|---|---|---|---|---|---|---|---|---|---|
| 2006-06-01 | -20.6% | 122 | -2.7% | +25.9% | +27.9% | 38% | 74 | 4 | yes |
| 2008-01-22 | -24.9% | 157 | -19.9% | -6.6% | +72.5% | 10% | 142 | 4 | yes |
| 2016-02-11 | -20.5% | 220 | +49.9% | +25.3% | +32.3% | 81% | 19 | 11 | yes |
| 2020-03-12 | -21.9% | 255 | +35.4% | +37.1% | +42.3% | 54% | 111 | 5 | yes |
