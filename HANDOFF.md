# Handoff

For a fresh session picking this project up. Written so nothing here has to be
re-derived.

Last updated: 2026-09-23 (deployed and running)

---

## What this is

A buy-the-dip research dashboard for Indian equities, for delivery-based
long-term investing. Scans the Nifty 500 for stocks that have fallen inside an
ongoing uptrend, confirms someone is accumulating, sizes the position, and
defines the exit before entry.

**It is a research tool, not investment advice, and the owner is not a
SEBI-registered adviser.** Every verdict screen and alert carries that line.
Keep it that way.

Run it: `.venv/Scripts/python.exe -m streamlit run app.py`
Test it: `.venv/Scripts/python.exe -m pytest tests/ -q` (250 passing)

Windows, Python 3.12, venv at `.venv`. Machine has 7.7 GB RAM and no
dedicated GPU — **a local LLM is not viable here**, don't propose one.

---

## Decisions already made, and why

These were settled with the owner. Don't relitigate them without a reason.

| Decision | Why |
|---|---|
| Rule-based committee, no LLM | Owner chose free. Nine of 23 bots are pure arithmetic anyway; rules are backtestable and the learning loop wants determinism. A seam exists to add LLM bodies later. |
| FIFO lot accounting | Indian tax law requires it for listed equity. Not a modelling preference. |
| Transactions are the source of truth | Staged entries (3 tranches) and trims mean a position is never one price on one date. |
| Charges folded into cost basis | The sizer estimates win rate and payoff from the trade ledger; gross figures would inflate position sizes over time. |
| Streamlit Cloud + GitHub Actions + Telegram | Owner chose free hosting. Code public, data and keys private. |
| Conviction drives the Kelly fraction, not the odds | All three bands share one prior until real trades prove conviction predicts. |

## Numbers that were measured, not guessed

Don't re-estimate these; they came from real data.

- **Evidence pack**: ~3,400 tokens per stock (not the 20k first assumed).
- **API cost if ever added**: all-Opus ≈ ₹97/deep dive, all-Sonnet ≈ ₹39,
  hybrid (10 bots as rules + 13 on Haiku) ≈ ₹13. Caching saves 21%, not more —
  the pack is small enough that output tokens dominate.
- **Delivery ratio distribution** across 60 Nifty 500 stocks: p25 0.99,
  p50 1.02, p75 1.07, p90 1.11, max 1.14. The threshold is 1.05 (~p65) because
  1.10 sat at p90 and passed 3% of stocks, which after a ~5% dip screen would
  never fire.
- **Backtest**, walk-forward, no look-ahead, vs Nifty 500 buy-and-hold:

  | Window | Strategy | Index | Strategy DD | Index DD |
  |---|---|---|---|---|
  | 2020-2026 | 21.8% | 16.3% | -14.5% | -18.8% |
  | 2017-2023 | 18.4% | 14.1% | -27.8% | -38.3% |
  | 2018-2021 | 9.3% | 10.7% | -33.8% | -38.3% |

- **The trend filter is the strategy.** A/B over 2018-2021: with it +9.0% CAGR
  and -31.6% drawdown; without it **-3.6% CAGR and -50.9% drawdown**.
- **The screen score does not predict outcomes.** Bucketing 114 backtest trades
  by it gave the top bucket a 51% win rate against the bottom's 67%. It is a
  reading order, not a quality ranking. Conviction is meant to be the
  predictive number — verifying that is an open task.
- **Cold-start sizing priors**: p = 0.614, b = 1.96, from the 2017-2023 window.
  All three conviction bands share them deliberately.

## Bugs already found and fixed — don't reintroduce

- **Falling-knife leak.** The trend filter originally checked the 200 DMA slope
  only when price was *below* the average, so a crashed stock that bounced
  above its own falling average passed. The slope condition is now
  unconditional, in both `screener.evaluate_dip` and `backtest.dip_signal_at` —
  **these two must stay identical or the backtest stops testing the live rule.**
- **Benchmark misalignment.** `prices.get_index_history` always ends at today,
  so asking for "6 years" on a window closing in 2023 fetched 2020-2026 and
  sliced it, reporting a COVID-recovery CAGR as the benchmark. Now measured
  from today and discarded if it covers <90% of the window.
- **Vacuous quality-gate pass.** A delisted symbol with every metric `None`
  skipped every check and "passed". Now needs ≥3 of 5 gate metrics.
- **NSE date filters silently return empty.** `corporates-pit` ignores
  `from_date`/`to_date` and returns nothing. Fetch unfiltered, filter in pandas.
- **`enableCORS=false`** was left in the Streamlit config, accepting any
  origin. Removed.
- **The Overview presented the screen score as a verdict.** The committee was
  built but never wired into the scan, so the dashboard's most prominent number
  was `screen_score` - the one measured as *not* predicting outcomes - shown as
  "score 51/100" under a tagline reading "sized by conviction". Worse, for the
  live candidate the screen score (51) and the committee's conviction (51)
  coincided exactly. `scan.run_scan` now runs the committee on the top N
  survivors, `scan.convictions_for()` fetches the verdicts, and the Overview
  leads with stance and conviction while the screen score is demoted to a
  labelled annotation. A candidate with no verdict is shown as NOT ASSESSED
  rather than defaulting to neutral.

  **The general rule this came from:** whatever the dashboard shows largest is
  read as the recommendation, whatever the caption says. Do not display a
  number that is known not to predict in the position a verdict belongs.
- **Routine compliance filings were scored as governance crises.** The event
  taxonomy matched bare `sebi`, so "Certificate under SEBI (Depositories
  and Participants) Regulations, 2018" - filed quarterly by every listed
  company - classified as a governance event at materiality 1.0. Red-flag
  counts ran to 12-18 per stock and the news desk was dragged negative across
  the whole universe. Governance patterns now require an adverse *action*
  (order, penalty, show cause, investigation, resignation), `results` no longer
  matches a bare "quarter", and an auditor *change* is separated from an
  auditor *resignation*. After the fix: flags 12-18 → 0-4, news desk -1.30 →
  -0.57, conviction +2 to +4. Regression tests in `TestEventTaxonomy`.
- **The access gate failed open.** `is_deployed()` keyed on `DATABASE_URL`
  being set, so deploying with `DASHBOARD_PASSWORD` configured but that one
  missing served the portfolio publicly with no password - the opposite of
  what its docstring claimed. It now assumes deployed unless proven local and
  requires `ALLOW_INSECURE_LOCAL=1` for the open path.
  `dashboard_access_mode()` returns open/password/**refuse**, three states, so
  "no password configured" is distinguishable from "no password needed".
  Regression tests in `tests/test_access.py`.
- **A 404 was retried three times.** `cached_fetch` treated a missing bhavcopy
  (market holiday, or not yet published) as a transient failure: twelve wasted
  seconds and ERROR lines for something working correctly. `cache.NotFound` now
  short-circuits the retry loop.
- **pg8000 sends one round trip per row, and it looks exactly like a hang.**
  The first scheduled run was killed by the job timeout after 29 minutes
  stuck on its first step. The obvious theory - NSE blocking datacenter IPs -
  was wrong: `jobs/diagnose_network.py` showed the GitHub runner fetching the
  bhavcopy in 0.4s. The cost was the *write*. pg8000 has no fast executemany,
  so `insert(), [list of dicts]` costs a round trip per row, and the runner
  (Dulles) is ~250ms from Neon (Singapore). Writing ~3,500 delivery rows was
  fifteen minutes of silence.

  **Never pass a list of dicts to `insert()` in this codebase.** Use
  `insert().values(chunk)` so it becomes one statement. Applied in
  `nse.store_delivery_bars`, `committee.persist_report` and `scan.run_scan`.
  Delivery is also restricted to the screening universe - the bhavcopy holds
  every NSE equity and ~2,900 of them are rows nothing reads.

  `jobs/diagnose_network.py` exists for exactly this class of problem: it
  probes DNS, TCP, NSE, Yahoo and the database with hard timeouts and says
  which one is slow. Run it before theorising.
- **Streamlit Cloud defaults to Python 3.14; this project needs 3.12.**
  The first deploy crashed with a TypeError inside
  `metadata.create_all()`. Every pin here was tested on 3.12, and pg8000
  1.31.5 - the newest release - declares support only to 3.13 (SQLAlchemy
  2.0.54 does claim 3.14, so pg8000 is the suspect rather than a confirmed
  cause; the full traceback was never captured). **`runtime.txt` is ignored
  by Streamlit Cloud** (streamlit/streamlit#15326), and the Python version
  cannot be changed after deploy - the app must be deleted and recreated
  with the version set in Advanced settings. If a future redeploy breaks
  mysteriously, check the Python version first.
- **The password gate shipped with a NameError.** Rewriting `authenticated()`
  to use `dashboard_access_mode()` removed the line binding `password` but
  left `if entered == password` using it, so every login raised. Nothing
  caught it because `app.py` needs a Streamlit runtime to import, so no test
  touched it. `tests/test_access.py::TestLoginPathIsExecutable` now parses
  app.py with `ast` and checks the login path for undefined names - static,
  so it needs no runtime. The comparison also moved to
  `secrets.compare_digest`, with an explicit empty-input guard because
  `compare_digest("", "")` is True.
- **Red flags were double-counted.** The Bear Case Analyst re-emits other bots'
  flags with the source prefixed, so each arrived twice. The count feeds the
  sizing band's red-flag test, so a duplicate could shrink a position for no
  reason. `cmio._dedupe_flags` matches on the text after the source prefix.

---

## Architecture

```
app.py                  Router + password gate (st.navigation)
views/                  overview, screener, deep_dive, committee, portfolio, backtest_page
src/
  config.py             Loads config.yaml; secrets from env, never the file
  indicators.py         RSI/ATR/DMA/drawdown/delivery — Wilder smoothing, pure functions
  screener.py           Stages 1-3
  scan.py               Runs a scan, persists it
  portfolio.py          FIFO lots, derived position state, realised gains
  charges.py            Indian delivery-equity cost model
  db.py                 SQLAlchemy Core schema
  data/                 provider.py (DataResult contract), nse.py, prices.py,
                        fundamentals.py, cache.py
  importers/groww.py    Broker file reader
  strategy/             sizing.py, exit.py, backtest.py
  agents/               The committee: schemas, base (the seam), evidence,
                        lexicon, the 5 desk files, leads, cmio, registry
  committee.py          Orchestrator: three waves, persists every verdict
  ui/theme.py           Styling + Indian number formatting
```

**Two contracts hold the system together:**

`DataResult` (`src/data/provider.py`) wraps every fetch so "we could not get
this" travels as data, not an exception. `status` distinguishes OK / STALE /
PARTIAL / UNAVAILABLE / NOT_APPLICABLE. The difference between "there were no
insider trades" and "NSE was down" changes a verdict, so never collapse them.

`config.yaml` holds every threshold with a comment explaining it. Change
numbers there, not in code.

## Data sources — what works and what doesn't

NSE blocks plain `requests` at the TLS layer (403). `curl_cffi` impersonating
Chrome gets through, and the session must load the homepage for cookies first.
All of this is handled in `src/data/nse.py` — use `nse.get_session()`.

Working: index constituents, bhavcopy (incl. `DELIV_QTY`/`DELIV_PER`),
`corporates-pit` (insider), shareholding pattern, corporate announcements,
bulk/block deal CSVs, F&O ban list, equity master.

**Not reachable**: NSE short-selling (503) and the ASM/GSM surveillance lists.
Bots depending on them must report `data_available: false`. Don't fake it.

Yahoo Finance supplies long price history and fundamentals.
`get_price_history_batch` is 49× faster than per-symbol — always use it for
multi-stock work (500 stocks: 45s vs 37min).

---

## State of play

**Done and tested**
- Data layer, caching, rate limiting
- Indicators (checked against Wilder's published series)
- Three-stage screener
- Position sizing: conviction-weighted fractional Kelly + ATR overlay
- Exit doctrine incl. per-lot LTCG tracking
- Walk-forward backtest with A/B on the trend filter
- Transaction ledger with FIFO, charges, Groww import
- Dashboard: five pages, dark theme, live

- **The 23-bot committee** (`src/agents/`, `src/committee.py`). Rule-based,
  runs in ~9 seconds, costs nothing. `Analyst` ABC splits `gather` (arithmetic,
  permanent) from `judge` (the seam). Adding an LLM later means subclassing one
  bot and overriding `judge` - no other file changes.

**Committee design points worth not breaking**
- A blind bot contributes **zero weight**, not a zero score. `Verdict.weight`
  returns 0 when `data_available` is False, so a desk with two working bots is
  weighted on those two rather than diluted by two neutral non-votes.
- `ctx.reachable()` gates whether a bot runs; `ctx.has()` asks whether there
  are rows. The distinction matters: "no insider disclosures were filed" is a
  finding, "NSE was unreachable" is not. Gating on `has()` silenced the insider
  bot's correct handling of an empty-but-successful fetch.
- The Research Validation Analyst is the one bot that reports at zero coverage.
  Auditing the absence of evidence is its function.
- Bull and Bear each see only evidence supporting their own side, so they
  cannot converge. A test asserts they land on opposite signs.
- The CMIO does not re-derive sizing. It hands conviction to
  `sizing.size_position` and `exit.build_doctrine`, so there is one
  implementation of how much to buy.

- **Deployment layer.** `src/alerts/telegram.py`, `jobs/daily_scan.py`,
  `jobs/keepalive.py`, and both GitHub Actions workflows. Built and dry-run
  locally; nothing is deployed yet.

**Deployment decisions already made**
- Public GitHub repo, Neon Postgres, Streamlit Community Cloud, GitHub Actions
  cron at 19:00 IST weekdays, Telegram alerts.
- Alerts fire on committee **BUY only** (conviction >= 60), plus every EXIT and
  TRIM on holdings, LTCG deadlines weekly, and scan failures. A WATCH does not
  alert - the committee declining to recommend should not train you to act.
- **pg8000, not psycopg2.** psycopg2's compiled extension is blocked by
  Application Control on the dev machine. pg8000 is pure Python and works in
  both places. `config.database_url()` rewrites the scheme to
  `postgresql+pg8000://` and strips libpq's `?sslmode=require`, which pg8000
  rejects; TLS is applied through `connect_args` in `db.get_engine()` instead.
  Stripping it there and not applying it here would connect in the clear.
- **Requirements are pinned**, not ranged. Streamlit Cloud resolves fresh on
  every rebuild.

**Not started**
- Nothing is actually deployed - see "Deploying" below (`src/alerts/telegram.py`)
- Scheduled daily scan (`.github/workflows/`, `jobs/`)
- The self-learning loop (`src/learning/`) — schema exists, no code
- Deployment to Streamlit Cloud + Postgres

## Does conviction predict returns? Measured 2026-09-23. No.

This was the last open question of substance and it now has an answer, so do
not re-open it from scratch — reproduce it with `jobs/validate_conviction.py`.

**Method.** 4,321 point-in-time observations, 150 Nifty 500 symbols, six years,
sampled every 21 sessions and *only* on bars where the dip screen would have
looked. Every signal recomputed from bars up to the entry date. Returns are
excess over the Nifty 500.

**Result.**

| signal | 21d | 63d | 126d | verdict |
|---|---|---|---|---|
| `dip_conviction` | -0.001 | -0.013 | -0.006 | zero |
| `screen_score` | -0.010 | -0.013 | -0.022 | zero, slightly negative |
| `drawdown_pct` | +0.003 | -0.004 | -0.009 | zero |
| `rsi` | +0.013 | +0.019 | +0.010 | zero |
| `dma_slope_pct` | +0.013 | +0.007 | -0.001 | zero |
| `pct_vs_dma_long` | +0.012 | +0.005 | +0.033 | weak |
| `atr_pct` | +0.038 | +0.062 | +0.081 | see below |

The conviction quintile spread is **negative at all three horizons** (-0.07%,
-0.43%, -0.71%). The highest-conviction bucket did not beat the lowest.

**Three corrections were applied, and each one mattered.**

1. *Multiple comparisons.* Twenty-one tests at p<0.10 produce about two false
   positives on pure noise. Under Bonferroni only `atr_pct` at 63d and 126d
   survived.
2. *Cross-sectional IC.* Pooling across dates confuses "this month was good"
   with "this signal is good". Measuring within each month and averaging the
   71 monthly ICs is the honest version. It promoted `pct_vs_dma_long` (+0.085
   at 126d, t +4.38, positive in 71% of months) and left conviction at +0.012.
3. *Regime split.* `atr_pct` reads **+0.108 when the market rose and -0.053
   when it fell**. The sign flips: that is beta, not skill. The only signal
   that survived correction was measuring market exposure. Discount it.

**The one finding worth acting on.** `pct_vs_dma_long` — distance above the
200-DMA — has a genuine within-month IC of +0.085 at 126 sessions and does not
flip sign in falling markets (+0.003). Among dip candidates the *shallower*
dips outperform. `drawdown_pct` agrees by having a negative cross-sectional IC.
This is the opposite of what "buy the deeper dip" assumes, and the conviction
formula currently pushes both ways at once, which is part of why it nets to
zero.

**The worst finding.** In falling markets `dip_conviction` is *inverted* —
-0.073 at 63 sessions (p 0.004) and -0.067 at 126 (p 0.015). High conviction
picked worse stocks exactly when it mattered most.

**What this does and does not license.**

- It does **not** say the system is worthless. Conviction still gates entry
  (WATCH vs BUY) and caps size. Those are threshold decisions, not ranking
  decisions, and were not tested here.
- It **does** say that sizing *proportionally* to conviction is currently
  sizing proportionally to noise. `cold_start` already uses one prior for all
  three bands for exactly this reason; keep it that way.
- It says nothing at all about the equity, ownership and news desks, which
  carry 75% of desk weight and **cannot be backtested** — fundamentals,
  shareholding, insider filings and news are only available as they stand
  today. The live `committee_runs` ledger is the only honest route: conviction
  is recorded at every run, so check it against forward returns at the
  30/90/180-day marks as real time passes.

**scipy is unavailable on this machine.** Its compiled extensions are blocked
by the same Windows Application Control policy that blocked psycopg2. Spearman
and the p-values in `src/learning/attribution.py` are implemented directly on
numpy/pandas ranks with an erf-based normal approximation. Do not reintroduce
a scipy import.

## Open questions

- **Does conviction predict returns?** Still open, and now the most important
  question in the project. The screen score does not predict. The committee is
  deterministic precisely so this can be measured: run it across historical
  dates, correlate conviction against forward returns, report the information
  coefficient. Historical ownership and news coverage is thin, so a backtest
  can only validate the price/delivery/fundamental bots - say so rather than
  implying full coverage. If conviction fails the same way the screen score
  did, the desk weights need rethinking before any of this is trusted.
- **The Groww importer is unverified against a real file.** Built against a
  synthetic one with alias matching and a manual column mapper. Ask the owner
  for an actual export.
- **Historical committee backtesting is limited.** Shareholding and insider
  data aren't available point-in-time, so a historical run can only validate
  the price/delivery/fundamental bots. Say so rather than implying full coverage.

## Working style the owner expects

- Numbers get measured, not assumed — and when an earlier estimate was wrong,
  say so plainly and correct it.
- Thresholds are calibrated against observed distributions, and the percentiles
  go in the config comment.
- Caveats are stated up front, not buried. A backtest that hides survivorship
  bias is worse than none.
- Design skills in the owner's CLAUDE.md apply to frontend work — see the table
  there for which skill covers what.


---

## Deployed and running

Live as of 2026-09-23. All of it on free tiers.

| Piece | Where |
|---|---|
| Repo | github.com/neevoswal19-maker/dip-committee (public) |
| Database | Neon Postgres, ap-southeast-1, pooled endpoint |
| Dashboard | Streamlit Community Cloud, **Python 3.12** |
| Scheduled scan | GitHub Actions, 19:00 IST weekdays |
| Alerts | Telegram @cmiostock_bot |

Verified end to end: a real Actions run scanned 120 stocks in 320s, wrote to
Neon, and delivered a Telegram summary. Exit 0.

**A full 500-stock run takes 15-20 minutes on a runner**, most of it the
20-requests-per-minute limiter in `cache.py`. The job timeout is 30 minutes,
so the headroom is thinner than it looks. If the scan ever starts timing out,
raise `timeout-minutes` before touching the rate limit - the limiter is what
keeps NSE serving us.

Secrets live in four places and must be kept in step: `.env` (local,
gitignored), `.streamlit/secrets.toml` (local, gitignored, TOML shape),
GitHub repository secrets, and Streamlit's Advanced settings.

### The original deployment sequence, for reference

1. **Push** - create a public GitHub repo, `git remote add origin ...`,
   `git push -u origin main`. One commit exists already.
2. **Neon** - create a project, copy the connection string.
3. **Backfill** - with `DATABASE_URL` set locally, run
   `python -c "from src.data import nse; print(nse.backfill_delivery_bars(90))"`
   so the first cloud scan reads delivery from the database instead of making
   sixty NSE requests.
4. **Telegram** - create a bot via @BotFather, message it, get the chat id from
   `api.telegram.org/bot<TOKEN>/getUpdates`.
5. **Secrets** - the same four in both GitHub repository secrets and Streamlit
   Advanced settings: `DATABASE_URL`, `DASHBOARD_PASSWORD`,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
6. **Streamlit Cloud** - deploy from the repo, main file `app.py`, and
   **set Python 3.12 in Advanced settings**. The default is 3.14 and it does
   not work; the version cannot be changed later without deleting the app.
   `.streamlit/secrets.toml` is generated locally (gitignored) in the TOML
   shape Streamlit's Secrets box expects.
7. **Verify** - trigger the Daily scan workflow manually from the Actions tab,
   confirm a Telegram message arrives, open the app and check the password
   gate holds and the candidates match.

`python jobs/keepalive.py` answers "is the deployed system still working" and
names what is wrong rather than just failing.

**Still untested: Postgres.** Every test so far has run on SQLite. Run the
suite once with `DATABASE_URL` pointing at Neon before trusting the deployment
- booleans, `nulls_last()` in `scan.candidates_for` and the JSON-in-Text
columns are the things most likely to differ.