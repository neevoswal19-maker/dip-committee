# Dip Committee

A buy-the-dip research dashboard for Indian equities, built for delivery-based
long-term investing.

**This is a research tool, not investment advice.** It is not operated by a
SEBI-registered adviser. Everything it produces is evidence and probability,
shown with its reasoning. The decision to buy or sell is yours.

---

## What it does

Scans the Nifty 500 every evening for stocks that have fallen inside an
ongoing uptrend, confirms that someone is actually accumulating the shares
rather than trading them, and tells you how much to buy and when to sell.

Three stages, cheapest first:

1. **Quality gate** - market cap, ROE, leverage, growth, years of profit
2. **Dip detection** - 10-35% off the 52-week high, RSI under 40, and a
   **rising 200-day average**
3. **Delivery confirmation** - elevated settled delivery on down days,
   sustained across sessions

The trend filter is the part that matters. Buying a stock that has fallen is
only sensible if the long-term trend is still up; without that condition the
same rules lose money through a crash.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env     # then fill in your keys
.venv/Scripts/python.exe -m streamlit run app.py
```

Nothing but the dashboard is needed to start - the screener, sizing, exit
doctrine and backtest all run without an API key.

## Pages

| Page | What it is for |
|---|---|
| **Overview** | What the most recent scan found |
| **Screener** | Run a scan; the funnel, the candidates, and why each near miss failed |
| **Deep Dive** | One stock in full: chart, fundamentals, forensics, ownership, and the position it would justify |
| **Committee** | 23 bots across five desks reach a conviction score, and the CMIO turns it into a position and an exit plan |
| **Portfolio** | Holdings, lots, the daily exit check, recording trades and importing from your broker |
| **Backtest** | Whether the rules actually beat holding the index |

## How position sizing works

Conviction-weighted fractional Kelly with a volatility overlay. Six steps,
each of which can only reduce the size:

1. Estimate win rate and payoff from the closed-trade ledger (backtest figures
   until enough real trades exist)
2. Raw Kelly - **a non-positive result is NO BUY regardless of the report**
3. Conviction picks the fraction: aggressive (half Kelly, 2.5% risk),
   balanced (quarter, 1.5%), conservative (eighth, 0.75%)
4. ATR overlay - take the smaller of the Kelly and volatility-based sizes
5. Portfolio constraints - per-stock, per-sector, position count, total heat
6. Split into staged tranches, because dips deepen

## Recording what you actually bought

Transactions are the source of truth. Holdings, average cost and realised
gains are all derived from them, so correcting a mistyped trade corrects
everything downstream.

This matters because the strategy never buys once: a position enters in three
tranches and leaves in trims, weeks or months apart. Storing a single average
would lose the individual lots - and with them the per-lot holding period that
Indian FIFO tax rules require. Buy in January, March and June and those shares
reach long-term treatment on three different dates; a sale disposes of the
oldest first, whether you intended that or not.

Three ways in:

- **Record a trade** - a form for each buy and sell, with brokerage, STT, GST,
  stamp duty and DP charges prefilled by a cost model and editable to whatever
  the contract note actually says
- **Import from broker** - upload a Groww order history (CSV or XLSX). Columns
  are auto-detected and shown for you to correct, company names are resolved to
  NSE symbols, and importing the same file twice adds nothing
- **Transactions** - the full ledger, with delete for corrections

Charges are folded into the cost basis, so a purchase costs slightly more than
the traded price and a sale returns slightly less. Both the gross and net
figures are kept, because the sizer's win rate and payoff estimates are built
on this table and should reflect what you really netted.

## When to sell

An exit doctrine is written at entry and re-checked daily: thesis-break rules
that sell regardless of price, staged profit-taking, a wide trailing stop, a
valuation exit, and a hard stop that forces a re-review rather than an
automatic sale.

Indian tax treatment is built in. Gains within twelve months are taxed at 20%
against 12.5% beyond, so every holding shows its days to long-term treatment
and the rupee cost of selling early.

## Backtest results

Walk-forward, no look-ahead, measured against Nifty 500 buy-and-hold:

| Window | Strategy | Index | Strategy max DD | Index max DD |
|---|---|---|---|---|
| 2020-2026 | 21.8% | 16.3% | -14.5% | -18.8% |
| 2017-2023 | 18.4% | 14.1% | -27.8% | -38.3% |
| 2018-2021 | 9.3% | 10.7% | -33.8% | -38.3% |

Better than the index in two windows of three, with a shallower drawdown in
all three.

**Caveats, because a backtest that hides them is worse than none:** the
universe is today's index membership, so companies that dropped out are
missing and returns are flattered - budget roughly 2 percentage points of CAGR
for that. Stage 3 delivery is not applied historically, since it needs one NSE
request per session; because it only removes candidates, these figures are a
floor. Past performance is not a forecast.

## Data sources

All free, all unofficial. NSE blocks plain HTTP clients at the TLS layer, so
requests go through `curl_cffi` impersonating Chrome.

| Source | Used for |
|---|---|
| NSE index constituent files | The universe, with sectors |
| NSE bhavcopy | Prices and **deliverable quantity** |
| NSE corporate filings | Insider disclosures, shareholding, announcements |
| NSE archives | Bulk and block deals, F&O ban list |
| Yahoo Finance | Long price history, fundamentals, statements |

Short-selling and the ASM/GSM surveillance lists are not reachable; anything
depending on them reports that the data is missing rather than guessing.

## Layout

```
app.py              Router, auth gate
views/              The five dashboard pages
src/
  config.py         Everything tunable lives in config.yaml
  indicators.py     RSI, ATR, DMA, drawdown, delivery statistics
  screener.py       The three-stage screen
  scan.py           Running a scan and persisting it
  db.py             Schema: scans, verdicts, transactions, positions, trades
  portfolio.py      FIFO lots, derived position state, realised gains
  charges.py        Indian delivery-equity cost model
  data/             Fetchers, cache and rate limiter
  importers/        Broker trade-file readers
  strategy/         sizing.py, exit.py, backtest.py
  ui/theme.py       Styling and formatting helpers
tests/              208 tests
```

## Configuration

Every threshold is in `config.yaml` with a comment explaining it. Several were
set from measured distributions rather than intuition - the delivery threshold,
for instance, is calibrated against the observed spread across the index, and
the comment records the percentiles it was chosen from.

## The committee

Twenty-three bots: seventeen analysts across five desks, five desk heads, and a
Chief Market Intelligence Officer. All rule-based, so a run takes about nine
seconds and costs nothing.

Nine of them were never language problems - delivery statistics, insider net
flow, shareholding deltas, ratios, forensics, post-result drift, sector
relative strength, news-to-price-gap correlation, coverage auditing. Those are
the same computation a model would be asked to narrate. Five more use lookup
tables and a finance lexicon in place of comprehension, and say so in their
findings rather than presenting a keyword count as reading.

Every bot splits `gather` (arithmetic) from `judge` (scoring). `judge` is the
seam: adding an LLM later means subclassing one bot, so you can switch on just
the news desk and measure whether it beats the rules it replaced.

Two rules hold it together. A bot with no data contributes zero *weight*, not a
zero score, so missing evidence widens uncertainty instead of voting "fine".
And the Financial Forensics Analyst holds a veto that overrides every other
bot, because the risk it guards against is permanent loss of capital rather
than underperformance.

## Still to come

The Telegram alerts, the scheduled daily scan,
and the self-learning loop that measures which bots were actually right and
re-weights them.
