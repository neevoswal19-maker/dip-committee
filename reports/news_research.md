# News catalysts: event study

Generated 2026-10-30 in 26 minutes. Filings from 2018-01-01 for 501 of today's Nifty 500 (0 failed to download), 386,494 filings, 6,076 catalyst events after de-duplication. Rules in src/strategy/news_catalyst.py, fixed before running: classify each filing from its NSE category and headline; buy at the next session's open; measure the return at 3, 6 and 12 months (63, 126, 252 sessions) after a 0.39% round trip, minus the Nifty 500 over the same dates.

**Pass criteria, per type, at the same horizon in both 2018-21 and 2022 onward:** at least 50 events; mean excess at least +3% at 6 months or +5% at 12 months; median above 0; beats the index in at least 55%; t-statistic at least 2.

**Read with these caveats:** today's Nifty 500 only, so companies that fell out are missing. Each filing is read from its category and headline only, without the PDF, so an order's size is unknown. Events cluster in time (one market move lifts many of them together), so the t-statistics overstate the certainty. Returns are before tax.

**Own-drift check (a diagnostic, not a pass criterion):** today's index members beat the index on an ordinary day too. 'After drift' subtracts each stock's own average excess, entered on every session of the same period, from its event's excess. What is left is the part the news explains.

## Decision: **nothing passed**

| Type | Result | Why it failed |
|---|---|---|
| order_win | fail | 12m 2018-21: median -5.7%; 12m 2018-21: beat the index 45% (need 55%) |
| acquisition | fail | 12m 2018-21: beat the index 52% (need 55%); 12m 2022+: beat the index 54% (need 55%) |
| capacity | fail | 12m 2018-21: median -0.0%; 12m 2018-21: beat the index 50% (need 55%) |
| approval_upgrade | fail | 12m 2018-21: median -12.5%; 12m 2018-21: beat the index 42% (need 55%) |

### order_win

From the category: 410; from the headline: 791. Events per year: 2018: 80, 2019: 75, 2020: 69, 2021: 94, 2022: 144, 2023: 135, 2024: 175, 2025: 232, 2026: 197

| Period | Horizon | Events | Mean excess | Median | Beat index | t | Mean after drift | t after drift |
|---|---|---|---|---|---|---|---|---|
| 2018-21 | 3 months | 318 | +1.3% | -3.7% | 42% | +0.9 | -0.9% | -0.7 |
| 2018-21 | 6 months | 318 | +3.4% | -4.6% | 42% | +1.6 | -2.8% | -1.4 |
| 2018-21 | 12 months | 318 | +18.6% | -5.7% | 45% | +3.8 | -4.4% | -1.0 |
| 2022+ | 3 months | 815 | +6.6% | +0.8% | 51% | +7.5 | -0.4% | -0.5 |
| 2022+ | 6 months | 756 | +16.4% | +4.0% | 57% | +9.6 | -0.8% | -0.5 |
| 2022+ | 12 months | 633 | +40.4% | +13.1% | 61% | +10.7 | -6.3% | -1.9 |

Examples (headline matches):

- 2023-10-26 ZENTEC: Zen Technologies Limited has informed the Exchange regarding a press release dated October 26, 2023, titled "ZEN TECHNOLOGIES LIMITED SECURES ORDER FROM MINISTR
- 2024-02-21 ZENTEC: Zen Technologies Limited has informed the Exchange regarding a press release dated February 21, 2024, titled "ZEN TECHNOLOGIES LIMITED SECURES ORDER OF Rs. 93 C
- 2024-10-01 ZENTEC: Zen Technologies Limited has informed the Exchange regarding a press release dated October 01, 2024, titled "Zen Technologies secures Annual Maintenance Contrac
- 2018-04-03 ZENSARTECH: Zensar Technologies Limited has informed the Exchange regarding a press release dated April 03, 2018, titled "Zensar Wins Multi-Year Contract with City of San D

### acquisition

From the category: 2208; from the headline: 881. Events per year: 2018: 202, 2019: 238, 2020: 192, 2021: 291, 2022: 353, 2023: 390, 2024: 518, 2025: 477, 2026: 428

| Period | Horizon | Events | Mean excess | Median | Beat index | t | Mean after drift | t after drift |
|---|---|---|---|---|---|---|---|---|
| 2018-21 | 3 months | 923 | +2.6% | -0.2% | 49% | +3.6 | -0.6% | -0.8 |
| 2018-21 | 6 months | 923 | +5.5% | -0.3% | 49% | +4.8 | -2.3% | -2.1 |
| 2018-21 | 12 months | 923 | +13.9% | +2.0% | 52% | +5.9 | -8.1% | -3.8 |
| 2022+ | 3 months | 2039 | +2.4% | +0.0% | 50% | +5.9 | -0.3% | -0.7 |
| 2022+ | 6 months | 1903 | +5.1% | +0.8% | 52% | +7.7 | -1.1% | -1.8 |
| 2022+ | 12 months | 1627 | +11.3% | +2.3% | 54% | +9.9 | -2.9% | -2.9 |

Examples (headline matches):

- 2019-01-30 ZYDUSWELL: Zydus Wellness Limited has informed the Exchange regarding a press release dated January 30, 2019, titled "Zydus Wellness completes acquisition of Heinz India".
- 2024-10-30 ZYDUSWELL: Zydus Wellness Limited has informed the Exchange regarding a press release dated October 30, 2024, titled "Zydus Wellness to acquire 100% stake in Naturell (Ind
- 2024-11-30 ZYDUSWELL: Zydus Wellness Limited has informed the Exchange about acquisition of 98.14% equity share capital of Naturell (India) Private Limited
- 2025-08-29 ZYDUSWELL: Zydus Wellness Limited has informed the Exchange about Acquisition of Comfort Click Limited, United Kingdom

### capacity

From the category: 0; from the headline: 1202. Events per year: 2018: 59, 2019: 68, 2020: 53, 2021: 92, 2022: 131, 2023: 137, 2024: 183, 2025: 249, 2026: 230

| Period | Horizon | Events | Mean excess | Median | Beat index | t | Mean after drift | t after drift |
|---|---|---|---|---|---|---|---|---|
| 2018-21 | 3 months | 272 | +1.5% | -1.8% | 46% | +1.1 | -1.3% | -1.0 |
| 2018-21 | 6 months | 272 | +8.2% | -2.5% | 45% | +2.8 | -0.1% | -0.0 |
| 2018-21 | 12 months | 272 | +27.2% | -0.0% | 50% | +4.2 | -0.2% | -0.0 |
| 2022+ | 3 months | 862 | +2.3% | -1.0% | 47% | +3.6 | -0.9% | -1.5 |
| 2022+ | 6 months | 788 | +7.1% | +0.5% | 51% | +6.6 | -0.3% | -0.3 |
| 2022+ | 12 months | 653 | +13.4% | +3.9% | 56% | +7.5 | -2.3% | -1.4 |

Examples (headline matches):

- 2023-06-22 WIPRO: Wipro Limited has informed the Exchange regarding a press release dated Jun 22, 2023, titled "Wipro Expands Presence in South Africa, Inaugurates New Office in 
- 2024-02-13 ZFCVINDIA: ZF Commercial Vehicle Control Systems India Limited has informed the Exchange regarding 'Intimation under Regulation 30 of SEBI (Listing Obligations and Disclos
- 2024-04-15 ZEEL: Zee Entertainment Enterprises Limited has informed the Exchange regarding a press release dated April 15, 2024, titled "ZEE s MD & CEO inaugurates Breach Candy 
- 2026-07-06 ZENTEC: Zen Technologies Limited has informed the Exchange regarding a press release dated July 06, 2026, titled "Vector Technics Emerges as India's Largest Fully Integ

### approval_upgrade

From the category: 125; from the headline: 459. Events per year: 2018: 57, 2019: 57, 2020: 58, 2021: 42, 2022: 129, 2023: 57, 2024: 50, 2025: 69, 2026: 65

| Period | Horizon | Events | Mean excess | Median | Beat index | t | Mean after drift | t after drift |
|---|---|---|---|---|---|---|---|---|
| 2018-21 | 3 months | 214 | +1.2% | -2.8% | 47% | +0.8 | +1.4% | +0.9 |
| 2018-21 | 6 months | 214 | +4.3% | -3.8% | 44% | +1.7 | +3.6% | +1.6 |
| 2018-21 | 12 months | 214 | +10.7% | -12.5% | 42% | +2.0 | +6.6% | +1.5 |
| 2022+ | 3 months | 349 | +4.0% | +1.4% | 54% | +4.2 | +0.3% | +0.4 |
| 2022+ | 6 months | 329 | +9.3% | +3.2% | 58% | +6.3 | +0.5% | +0.3 |
| 2022+ | 12 months | 292 | +26.4% | +15.4% | 66% | +8.8 | +5.2% | +2.0 |

Examples (headline matches):

- 2026-04-08 ZYDUSLIFE: Zydus Lifesciences Limited has informed the Exchange regarding a press release dated April 08, 2026, titled "Zydus receives final approval from USFDA for Dapagl
- 2026-05-28 ZYDUSLIFE: Zydus Lifesciences Limited has informed the Exchange regarding a press release dated May 28, 2026, titled "Zydus Therapeutics  New Drug Application (NDA) for Sa
- 2026-08-01 ZYDUSLIFE: Zydus Lifesciences Limited has informed the Exchange regarding a press release dated August 01, 2026, titled "Zydus receives EIR from the USFDA for its injectab
- 2026-10-21 ZYDUSLIFE: Zydus Lifesciences Limited has informed the Exchange regarding a press release dated October 21, 2026, titled "Zydus receives USFDA approval for Estradiol and L
