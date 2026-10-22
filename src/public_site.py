"""The public demo page: a read-only snapshot of the research, for anyone.

Built by jobs/public_site.py after each morning scan and intraday session and
published to GitHub Pages. It shows what the system found, never what the
owner holds:

  shown       the latest scan funnel, the committee's verdicts on it, recent
              BUY calls, the market trend, the intraday paper test, and the
              backtest summary
  never read  positions, transactions, trades, sizing, capital or any secret

`gather` reads only the scans, candidates, committee_runs, bot_verdicts,
stocks and intraday_trades tables; `render` turns that into one static HTML
file with no scripts. tests/test_public_site.py plants a holding in the
database and checks it never reaches the page.
"""

from __future__ import annotations

import html
import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable

from sqlalchemy import desc, select

from src import db

log = logging.getLogger(__name__)

REPO_URL = "https://github.com/neevoswal19-maker/dip-committee"
CALL_WINDOW_DAYS = 60

#: From reports/option1_research.md: the live rules, 2020 to 2026, Rs 5 lakh,
#: Groww costs, entry at the next open. Growth a year / worst fall.
BACKTEST = [
    ("Dip rules, 8% positions (live sizing)", "17.9%", "-33.9%"),
    ("Dip rules with delivery confirmation", "15.2%", "-32.5%"),
    ("Nifty 50, bought and held", "9.9%", "-38.4%"),
]

REJECTED = [
    ("Swing trading on the Nifty 50",
     "Six published strategies won 59 to 71% of trades, but Indian delivery costs of about 0.46% a "
     "trade left them near break-even."),
    ("The same dip rules on US stocks",
     "Kept pace with SPY until 2021, then fell well behind it. Not deployed."),
    ("ETF relative-strength rotation",
     "Lost money from 2014 to 2021 on gold and silver; sector rotation missed its bar."),
]


# --- Gathering --------------------------------------------------------------------------


def gather(cfg: Any, *, regime_fn: Callable[[], Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Everything the page shows, as plain data. Reads no portfolio table."""
    from src import scan
    from src.intraday import orb

    db.init_db()
    now = now or db.now()
    snapshot: dict[str, Any] = {"generated": now, "scan": None, "candidates": [], "calls": [],
                                "market": None, "intraday": {}, "intraday_trades": []}

    latest = scan.latest_scan()
    if latest:
        snapshot["scan"] = {k: latest.get(k) for k in ("trade_date", "universe_size", "passed_dip",
                                                       "passed_quality", "passed_delivery", "run_at")}
        rows = scan.candidates_for(latest["id"])
        verdicts = scan.convictions_for([r["symbol"] for r in rows]) if rows else {}
        sectors = _sectors([r["symbol"] for r in rows])
        for row in rows:
            v = verdicts.get(row["symbol"].upper(), {})
            snapshot["candidates"].append({
                "symbol": row["symbol"], "sector": sectors.get(row["symbol"]),
                "close": row.get("close"), "drawdown_pct": row.get("drawdown_pct"),
                "stance": v.get("stance"), "conviction": v.get("conviction"), "valuation": v.get("valuation"),
            })

    snapshot["calls"] = _recent_calls(now.date())
    snapshot["market"] = _market(regime_fn)

    with db.connection() as conn:
        paper = [dict(r._mapping) for r in conn.execute(
            select(db.intraday_trades).where(db.intraday_trades.c.mode == "paper")
            .order_by(desc(db.intraday_trades.c.entry_at)))]
    for row in paper:
        row["closed"] = bool(row.get("closed"))
    card = orb.scorecard(paper, float(cfg.get("intraday.capital", 500000)))
    verdict, why = orb.judge(card)
    snapshot["intraday"] = {**card, "verdict": verdict, "reasons": why, "needed": orb.PASS["min_trades"]}
    snapshot["intraday_trades"] = [{k: r.get(k) for k in ("day", "symbol", "side", "entry", "exit_reason", "r", "net",
                                                          "closed")} for r in paper[:8]]
    return snapshot


def _sectors(symbols: list[str]) -> dict[str, str]:
    if not symbols:
        return {}
    with db.connection() as conn:
        rows = conn.execute(select(db.stocks.c.symbol, db.stocks.c.sector)
                            .where(db.stocks.c.symbol.in_(symbols))).fetchall()
    return {r.symbol: r.sector for r in rows if r.sector}


def _recent_calls(today: date) -> list[dict[str, Any]]:
    """The first BUY call on each stock in the window, newest first."""
    from src import scan

    since = today - timedelta(days=CALL_WINDOW_DAYS)
    with db.connection() as conn:
        rows = conn.execute(
            select(db.committee_runs.c.id, db.committee_runs.c.symbol, db.committee_runs.c.trade_date,
                   db.committee_runs.c.price_at_run, db.committee_runs.c.conviction)
            .where(db.committee_runs.c.stance == "BUY")
            .where(db.committee_runs.c.trade_date >= since)
            .order_by(db.committee_runs.c.id)).fetchall()
        first: dict[str, Any] = {}
        for r in rows:
            first.setdefault(r.symbol, r)
        calls = []
        for r in first.values():
            calls.append({"symbol": r.symbol, "trade_date": r.trade_date, "price": r.price_at_run,
                          "conviction": r.conviction,
                          "valuation": scan._valuation_of(conn, r.id).get("valuation")})
    calls.sort(key=lambda c: (c["trade_date"] or date.min), reverse=True)
    return calls


def _market(regime_fn: Callable[[], Any] | None) -> dict[str, Any] | None:
    if regime_fn is None:
        return None
    try:
        market = regime_fn()
    except Exception as exc:              # the page must still build without it
        log.warning("Market trend unavailable: %s", exc)
        return None
    if market is None or not getattr(market, "known", False):
        return None
    return {"label": market.label, "pct_vs_dma": market.pct_vs_dma, "as_of": market.as_of}


# --- Rendering --------------------------------------------------------------------------


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _num(value: Any, digits: int = 2, prefix: str = "") -> str:
    if value is None:
        return '<span class="dim">-</span>'
    return f"{prefix}{float(value):,.{digits}f}"


def _date(value: Any, fmt: str = "%a %d %b") -> str:
    if not value:
        return "-"
    if isinstance(value, str):
        value = date.fromisoformat(value[:10])
    return value.strftime(fmt)


STANCE_CLASS = {"BUY": "buy", "WATCH": "watch", "NO_BUY": "no"}
STANCE_TEXT = {"BUY": "Buy", "WATCH": "Watch", "NO_BUY": "No buy"}


def _stance(stance: str | None) -> str:
    if not stance:
        return '<span class="chip">Not assessed</span>'
    return f'<span class="chip {STANCE_CLASS.get(stance, "")}">{_e(STANCE_TEXT.get(stance, stance))}</span>'


def _funnel(scan_row: dict[str, Any] | None, reviewed: int) -> str:
    if not scan_row:
        return '<p class="empty">No scan has completed yet.</p>'
    total = max(int(scan_row.get("universe_size") or 0), 1)
    steps = [
        ("Scanned", scan_row.get("universe_size") or 0, "the Nifty 500"),
        ("In a dip", scan_row.get("passed_dip") or 0, "10-35% off the high, uptrend intact"),
        ("Quality", scan_row.get("passed_quality") or 0, "returns, debt, profits, size"),
        ("Passed the screen", scan_row.get("passed_delivery") or 0, "sent to the committee"),
        ("Reviewed", reviewed, "by the 24 bots"),
    ]
    out = ['<ol class="funnel">']
    for i, (label, n, note) in enumerate(steps):
        width = max(2.0, 100.0 * int(n) / total)
        out.append(
            f'<li style="--i:{i}"><div class="funnel-head"><span class="funnel-n">{int(n):,}</span>'
            f'<span class="funnel-label">{_e(label)}</span></div>'
            f'<div class="bar"><span style="width:{width:.1f}%"></span></div>'
            f'<p class="funnel-note">{_e(note)}</p></li>')
    out.append("</ol>")
    return "".join(out)


def _market_line(market: dict[str, Any] | None) -> str:
    if not market:
        return ""
    pct = market.get("pct_vs_dma")
    where = (f"{abs(pct):.1f}% {'above' if pct >= 0 else 'below'} its 200-day average"
             if pct is not None else "against its 200-day average")
    label = str(market.get("label", "")).lower()
    return (f'<p class="market"><span class="dot {_e(label)}"></span>Market trend: '
            f'<strong>{_e(label)}</strong>. The Nifty 500 is {where}.</p>')


def _candidates(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ('<p class="empty">No stock passed the screen in this session. Quiet days are normal: '
                'most of the Nifty 500 is either not in a dip or not in an uptrend.</p>')
    body = []
    for r in rows:
        body.append(
            f'<tr><td class="sym">{_e(r["symbol"])}<span class="sub">{_e(r.get("sector") or "")}</span></td>'
            f'<td class="n">{_num(r.get("close"), 2, "&#8377;")}</td>'
            f'<td class="n">{_num(r.get("drawdown_pct"), 1)}%</td>'
            f'<td>{_stance(r.get("stance"))}</td>'
            f'<td class="n">{_num(r.get("conviction"), 0)}</td>'
            f'<td>{_e((r.get("valuation") or "").title()) or "<span class=dim>-</span>"}</td></tr>')
    return ('<div class="table"><table><thead><tr><th>Stock</th><th class="n">Price</th>'
            '<th class="n">Off high</th><th>Verdict</th><th class="n">Conviction</th><th>Valuation</th>'
            '</tr></thead><tbody>' + "".join(body) + "</tbody></table></div>")


def _calls(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return (f'<p class="empty">No buy calls in the last {CALL_WINDOW_DAYS} days. The committee buys only '
                'when a stock passes the screen and no bot objects.</p>')
    body = "".join(
        f'<tr><td>{_date(r.get("trade_date"))}</td><td class="sym">{_e(r["symbol"])}</td>'
        f'<td class="n">{_num(r.get("price"), 2, "&#8377;")}</td><td class="n">{_num(r.get("conviction"), 0)}</td>'
        f'<td>{_e((r.get("valuation") or "").title()) or "<span class=dim>-</span>"}</td></tr>' for r in rows)
    return ('<div class="table"><table><thead><tr><th>Session</th><th>Stock</th><th class="n">Price at call</th>'
            '<th class="n">Conviction</th><th>Valuation</th></tr></thead><tbody>' + body + "</tbody></table></div>")


def _intraday(card: dict[str, Any], trades: list[dict[str, Any]]) -> str:
    n, needed = card.get("trades", 0), card.get("needed", 40)
    verdict = card.get("verdict")
    status = ("Passed its test" if verdict else "Failed its test" if verdict is False
              else f"Paper trading: {n} of {needed} trades")
    stats = [
        ("Trades", f"{n} / {needed}"),
        ("Won", f"{card['win_rate']:.0%}" if n else "-"),
        ("Profit factor", "inf" if n and card.get("profit_factor") == float("inf")
         else f"{card['profit_factor']:.2f}" if n else "-"),
        ("Net on paper", f"&#8377;{card['net']:+,.0f}" if n else "-"),
    ]
    progress = min(100.0, 100.0 * n / max(needed, 1))
    out = [f'<div class="scorecard"><p class="status">{_e(status)}</p>'
           f'<div class="bar thin"><span style="width:{progress:.1f}%"></span></div><dl>']
    out += [f"<div><dt>{_e(k)}</dt><dd>{v}</dd></div>" for k, v in stats]
    out.append("</dl></div>")
    if trades:
        rows = "".join(
            f'<tr><td>{_date(t.get("day"))}</td><td class="sym">{_e(t.get("symbol"))}</td>'
            f'<td>{_e((t.get("side") or "").title())}</td><td class="n">{_num(t.get("entry"), 2, "&#8377;")}</td>'
            f'<td class="n {"pos" if (t.get("r") or 0) > 0 else "neg"}">{_num(t.get("r"), 2)}R</td>'
            f'<td>{_e((t.get("exit_reason") or "open").replace("_", " "))}</td></tr>' for t in trades)
        out.append('<div class="table"><table><thead><tr><th>Day</th><th>ETF</th><th>Side</th>'
                   '<th class="n">Entry</th><th class="n">Result</th><th>Exit</th></tr></thead><tbody>'
                   + rows + "</tbody></table></div>")
    else:
        out.append('<p class="empty">No paper trades yet. Setups are rare: about one every three sessions.</p>')
    return "".join(out)


def render(snapshot: dict[str, Any]) -> str:
    scan_row = snapshot.get("scan")
    candidates = snapshot.get("candidates", [])
    generated = snapshot["generated"]
    session = _date(scan_row.get("trade_date"), "%a %d %b %Y") if scan_row else "no scan yet"
    backtest_rows = "".join(f'<tr><td>{_e(a)}</td><td class="n">{b}</td><td class="n neg">{c}</td></tr>'
                            for a, b, c in BACKTEST)
    rejected = "".join(f"<li><strong>{_e(a)}.</strong> {_e(b)}</li>" for a, b in REJECTED)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dip Committee</title>
<meta name="description" content="A rule-based research committee that scans the Nifty 500 for quality stocks in a dip. Read-only snapshot, updated every weekday.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<main>
  <header class="hero">
    <div>
      <p class="eyebrow">Dip Committee</p>
      <h1>A research committee for quality Nifty 500 stocks in a dip</h1>
      <p class="lede">Every weekday before the market opens, it scans the Nifty 500 for companies that have
      fallen inside an uptrend, checks their quality, and puts each one to 24 rule-based bots covering
      fundamentals, forensic accounting, valuation, ownership and news. This page shows what it found.</p>
    </div>
    <aside class="meta">
      <dl>
        <div><dt>Latest session</dt><dd>{_e(session)}</dd></div>
        <div><dt>Page updated</dt><dd>{_e(generated.strftime("%d %b %Y, %H:%M"))} IST</dd></div>
        <div><dt>Source</dt><dd><a href="{REPO_URL}">GitHub</a></dd></div>
      </dl>
      <p class="note">Research output, not investment advice. Holdings and position sizes are private and
      not shown here.</p>
    </aside>
  </header>

  <section style="--s:1">
    <div class="section-head"><h2>Latest scan</h2>{_market_line(snapshot.get("market"))}</div>
    {_funnel(scan_row, len(candidates))}
  </section>

  <section style="--s:2">
    <div class="section-head"><h2>Committee verdicts</h2>
    <p class="hint">A stock that passed the screen is a buy unless the committee objects: a forensic
    accounting veto, or the bots net negative (conviction below 50).</p></div>
    {_candidates(candidates)}
  </section>

  <section style="--s:3">
    <div class="section-head"><h2>Buy calls, last {CALL_WINDOW_DAYS} days</h2>
    <p class="hint">The first call on each stock. Each one carried a stop-loss at -25% and profit-taking
    at +25% and +50%.</p></div>
    {_calls(snapshot.get("calls", []))}
  </section>

  <section style="--s:4">
    <div class="section-head"><h2>Intraday paper test</h2>
    <p class="hint">A separate strategy: an opening-range breakout followed by a pullback to VWAP, on
    NIFTYBEES and BANKBEES, 5-minute candles. It trades on paper until 40 trades are judged against a bar
    set in advance: profit factor of at least 1.3, a positive average after costs, and a worst fall no
    deeper than 10%.</p></div>
    {_intraday(snapshot.get("intraday", {}), snapshot.get("intraday_trades", []))}
  </section>

  <section class="split" style="--s:5">
    <div>
      <h2>How it was tested</h2>
      <p>Each rule was backtested on data available at the time, with Groww's real charges and entry at
      the next day's open. Delivery data was rebuilt from 1,759 daily NSE files. From 2020 to 2026:</p>
      <div class="table"><table><thead><tr><th></th><th class="n">Growth a year</th>
      <th class="n">Worst fall</th></tr></thead><tbody>{backtest_rows}</tbody></table></div>
      <p class="fine">Backtests use today's index members, so companies that failed and dropped out are
      missing, and returns are before tax. Read them as flattering. The committee itself cannot be
      backtested; its scores are being measured live.</p>
    </div>
    <div>
      <h2>What it said no to</h2>
      <p>The pass bar for every strategy is written down before it is tested. Three ideas failed and were
      never switched on:</p>
      <ul class="rejected">{rejected}</ul>
    </div>
  </section>

  <footer>
    <p>Built by Neev Oswal with Python, GitHub Actions, Neon Postgres, Streamlit and Telegram, on free tiers.
    <a href="{REPO_URL}">Source and research reports on GitHub</a>.</p>
    <p>Research output, not investment advice. Not a SEBI-registered adviser.</p>
  </footer>
</main>
</body>
</html>
"""


CSS = """
:root{--bg:#fafafa;--surface:#ffffff;--text:#18181b;--muted:#52525b;--faint:#a1a1aa;--line:#e4e4e7;
--accent:#0f766e;--accent-soft:#ccebe7;--pos:#15803d;--neg:#b42318;--warn:#a15c07;--chip:#f4f4f5;
color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0e0e11;--surface:#16161a;--text:#ececef;--muted:#a1a1aa;
--faint:#71717a;--line:#27272a;--accent:#5fb3a7;--accent-soft:#1d3a36;--pos:#6cc28b;--neg:#f0857a;
--warn:#e0a458;--chip:#1f1f24;color-scheme:dark}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.6 Geist,ui-sans-serif,system-ui,-apple-system,
"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:1080px;margin:0 auto;padding:56px 24px 72px}
a{color:var(--accent);text-underline-offset:3px}
h1{font-size:clamp(1.6rem,3.4vw,2.35rem);line-height:1.15;letter-spacing:-0.02em;font-weight:600;margin:0 0 16px;
max-width:22ch}
h2{font-size:1.05rem;letter-spacing:-0.01em;font-weight:600;margin:0}
p{margin:0}
.eyebrow{font:500 .78rem/1 "Geist Mono",ui-monospace,monospace;letter-spacing:.08em;text-transform:uppercase;
color:var(--accent);margin-bottom:18px}
.lede{color:var(--muted);max-width:62ch}
.hero{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(0,1fr);gap:48px;align-items:end;
padding-bottom:40px;border-bottom:1px solid var(--line)}
.meta{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:20px 22px}
.meta dl{margin:0 0 14px;display:grid;gap:10px}
.meta dl div{display:flex;justify-content:space-between;gap:16px}
dt{color:var(--muted);font-size:.86rem}
dd{margin:0;font:500 .86rem/1.5 "Geist Mono",ui-monospace,monospace;text-align:right}
.note,.fine,.hint,.funnel-note{color:var(--muted);font-size:.86rem}
.fine{margin-top:14px}
section{padding:40px 0;border-bottom:1px solid var(--line);animation:rise .6s cubic-bezier(.16,1,.3,1) both;
animation-delay:calc(var(--s,0)*70ms)}
.section-head{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.6fr);gap:24px;align-items:baseline;
margin-bottom:24px}
.market{font-size:.92rem;color:var(--muted)}
.market strong{color:var(--text);font-weight:500;text-transform:capitalize}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px;background:var(--faint);
vertical-align:1px}
.dot.uptrend{background:var(--pos)}.dot.downtrend{background:var(--neg)}.dot.mixed{background:var(--warn)}
.funnel{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:20px}
.funnel li{animation:rise .6s cubic-bezier(.16,1,.3,1) both;animation-delay:calc(var(--i)*60ms + 150ms)}
.funnel-head{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.funnel-n{font:500 1.6rem/1.1 "Geist Mono",ui-monospace,monospace;letter-spacing:-0.02em}
.funnel-label{font-size:.9rem;font-weight:500}
.bar{height:6px;border-radius:99px;background:var(--chip);overflow:hidden;margin:10px 0 8px}
.bar span{display:block;height:100%;border-radius:99px;background:var(--accent);transform-origin:left;
animation:grow .9s cubic-bezier(.16,1,.3,1) both;animation-delay:.3s}
.bar.thin{height:4px;margin:12px 0 20px}
.table{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--surface)}
table{width:100%;border-collapse:collapse;font-size:.92rem}
th{font-weight:500;color:var(--muted);font-size:.8rem;text-align:left;padding:11px 16px;border-bottom:1px solid var(--line);
white-space:nowrap}
td{padding:12px 16px;border-bottom:1px solid var(--line);white-space:nowrap}
tr:last-child td{border-bottom:0}
tbody tr{transition:background .2s}
tbody tr:hover{background:var(--chip)}
.n{text-align:right;font-family:"Geist Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
.sym{font-weight:600}
.sub{display:block;font-weight:400;color:var(--muted);font-size:.78rem}
.dim{color:var(--faint)}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.chip{display:inline-block;font-size:.78rem;font-weight:500;padding:3px 10px;border-radius:99px;background:var(--chip);
color:var(--muted)}
.chip.buy{background:var(--accent-soft);color:var(--accent)}
.chip.watch{color:var(--warn)}
.chip.no{color:var(--neg)}
.empty{color:var(--muted);padding:22px 20px;border:1px dashed var(--line);border-radius:12px;font-size:.92rem}
.scorecard{margin-bottom:20px}
.status{font-weight:500}
.scorecard dl{margin:0;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}
.scorecard dt{font-size:.8rem}
.scorecard dd{text-align:left;font-size:1.25rem;font-weight:500;margin-top:2px}
.split{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:56px}
.split h2{margin-bottom:12px}
.split > div > p{color:var(--muted);margin-bottom:18px;font-size:.95rem}
.rejected{margin:0;padding:0;list-style:none;display:grid;gap:16px}
.rejected li{padding-left:16px;border-left:2px solid var(--line);color:var(--muted);font-size:.92rem}
.rejected strong{color:var(--text);font-weight:500}
footer{padding-top:32px;color:var(--muted);font-size:.86rem;display:grid;gap:6px}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media (max-width:820px){main{padding:36px 16px 56px}.hero,.section-head,.split{grid-template-columns:1fr;gap:20px}
.hero{align-items:start}.funnel{grid-template-columns:repeat(2,minmax(0,1fr))}
.scorecard dl{grid-template-columns:repeat(2,minmax(0,1fr))}}
"""
