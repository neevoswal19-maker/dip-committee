"""Correction buying on paper, run by the morning job.

The rules are the short-correction backtest's (src/strategy/correction.py)
with config `correction:`: when the Nifty 500 closes 5% below its 3-month
high, buy every Nifty 100 stock that fell further, equal money each, at the
next open; sell each at its own 3-month high or after 6 months. Trades are
walked with the same `trade()` the backtest uses, so the paper record and the
backtest cannot drift apart.

Each morning: fill and walk the open trades, report exits, score any basket
that has finished, and alert on a new correction. Nothing is backfilled: a
correction that triggered before this started is not entered.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import func, select

from src import charges, db
from src.alerts import telegram
from src.strategy import correction as cr

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False, "mode": "paper", "index": "nifty500", "universe": "NIFTY 100", "depth_pct": 5.0,
    "window_sessions": 63, "max_hold_sessions": 126, "basket_capital": 500000.0, "slippage": 0.0005,
    "trigger_grace_sessions": 3,
}


def settings(cfg: Any) -> dict[str, Any]:
    return {**DEFAULTS, **(cfg.get("correction") or {})}


def load_data(s: dict[str, Any], session: date) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """The index and the universe's daily bars, up to the last completed session."""
    from src.data import nse, prices

    def completed(frame: pd.DataFrame | None) -> pd.DataFrame | None:
        if frame is None or frame.empty:
            return None
        frame = frame[frame.index <= pd.Timestamp(session)]
        return frame.dropna(subset=["open", "high", "low", "close"])

    idx = completed(prices.get_index_history(s["index"], years=2).value)
    universe = nse.get_universe(s["universe"]).value or []
    fetched = prices.get_price_history_batch(universe, years=2)
    frames = {sym: f for sym, r in fetched.items()
              if r is not None and (f := completed(r.value)) is not None and len(f) > 0}
    return idx, frames


def new_trigger(close: pd.Series, s: dict[str, Any]) -> cr.Episode | None:
    """The latest correction, if it triggered in the last few sessions."""
    eps = cr.rolling_episodes(close, float(s["depth_pct"]), int(s["window_sessions"]))
    if not eps:
        return None
    grace = int(s["trigger_grace_sessions"])
    recent = close.dropna().index[-grace:]
    return eps[-1] if eps[-1].trigger_date >= recent[0] else None


def picks_for(episode: cr.Episode, frames: dict[str, pd.DataFrame], s: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for symbol, frame in frames.items():
        p = cr.selected_short(frame, episode, int(s["window_sessions"]))
        if p:
            last = float(frame["close"][frame.index <= episode.trigger_date].iloc[-1])
            out.append({"symbol": symbol, "target": p["target"], "fall_pct": p["fall"] * 100,
                        "upside_pct": (p["target"] / last - 1) * 100})
    return out


def _costs(cfg: Any):
    return lambda side, qty, price: charges.estimate(side, qty, price, cfg=cfg).total


def walk_open_trades(frames: dict[str, pd.DataFrame], s: dict[str, Any], cfg: Any,
                     record: bool) -> list[dict[str, Any]]:
    """Fill entries at the open and close trades that hit their target or ran six months."""
    costs = _costs(cfg)
    exits = []
    with db.connection() as conn:
        rows = conn.execute(
            select(db.correction_trades, db.correction_episodes.c.trigger_date)
            .join(db.correction_episodes, db.correction_episodes.c.id == db.correction_trades.c.episode_id)
            .where(db.correction_trades.c.closed.is_(False))
        ).mappings().all()
        # The basket is split across every trade in the correction, closed ones included.
        totals = dict(conn.execute(
            select(db.correction_trades.c.episode_id, func.count())
            .group_by(db.correction_trades.c.episode_id)).all()) if rows else {}
        for r in rows:
            frame = frames.get(r["symbol"])
            if frame is None:
                log.warning("No prices for %s; its correction trade waits", r["symbol"])
                continue
            t = cr.trade(frame, r["symbol"], pd.Timestamp(r["trigger_date"]), r["target"], stop_pct=None,
                         max_hold=int(s["max_hold_sessions"]), slippage=float(s["slippage"]))
            if t is None:
                continue                                    # the next open has not happened yet
            each = float(s["basket_capital"]) / max(1, totals.get(r["episode_id"], 1))
            units = r["units"] or max(1, int(each // t.entry))
            values: dict[str, Any] = {"entry_date": t.entry_date.date(), "entry": t.entry, "units": units,
                                      "sessions": t.sessions, "updated_at": db.now()}
            if t.reason in ("target", "time"):
                net = units * (t.exit - t.entry) - costs("BUY", units, t.entry) - costs("SELL", units, t.exit)
                values.update(exit_date=t.exit_date.date(), exit=t.exit, reason=t.reason, net=net, closed=True)
                exits.append({"symbol": r["symbol"], "exit": t.exit, "exit_date": t.exit_date.date(),
                              "reason": t.reason, "gross_pct": t.gross_pct, "sessions": t.sessions})
            if record:
                conn.execute(db.correction_trades.update().where(db.correction_trades.c.id == r["id"]).values(**values))
    return exits


def finish_baskets(idx: pd.DataFrame, frames: dict[str, pd.DataFrame], s: dict[str, Any], cfg: Any,
                   record: bool) -> list[dict[str, Any]]:
    """Score every correction whose trades have all closed and whose index trade has ended."""
    costs = _costs(cfg)
    hold = int(s["max_hold_sessions"])
    done = []
    with db.connection() as conn:
        episodes = conn.execute(select(db.correction_episodes)
                                .where(db.correction_episodes.c.finished_on.is_(None))).mappings().all()
        for ep in episodes:
            trades = conn.execute(select(db.correction_trades)
                                  .where(db.correction_trades.c.episode_id == ep["id"])).mappings().all()
            if not trades or not all(t["closed"] for t in trades):
                continue
            episode = cr.Episode(float(s["depth_pct"]), pd.Timestamp(ep["peak_date"]), ep["peak_level"],
                                 pd.Timestamp(ep["trigger_date"]), ep["trigger_level"])
            it = cr.index_trade(idx["close"], idx["open"], episode, max_hold=hold)
            if it["return_pct"] is None or not (it["reached"] or it["sessions"] >= hold):
                continue                                    # the benchmark is still running
            cost = sum(t["units"] * t["entry"] + costs("BUY", t["units"], t["entry"]) for t in trades)
            proceeds = sum(t["units"] * t["exit"] - costs("SELL", t["units"], t["exit"]) for t in trades)
            every = [x for x in (cr.trade(f, sym, episode.trigger_date, float("inf"), stop_pct=None,
                                          max_hold=it["sessions"], slippage=float(s["slippage"]))
                                 for sym, f in frames.items()
                                 if int(f.index.searchsorted(episode.trigger_date, side="right")) >= cr.SHORT_MIN_HISTORY)
                     if x is not None]
            result = {
                "trigger_date": ep["trigger_date"], "trades": len(trades), "universe": s["universe"],
                "reached": sum(t["reason"] == "target" for t in trades), "net": proceeds - cost,
                "basket_pct": (proceeds / cost - 1) * 100, "index_pct": it["return_pct"],
                "every_stock_pct": cr.basket(every, costs, float(s["basket_capital"])).get("net_pct") or 0.0,
            }
            if record:
                conn.execute(db.correction_episodes.update().where(db.correction_episodes.c.id == ep["id"]).values(
                    finished_on=max(t["exit_date"] for t in trades), basket_pct=result["basket_pct"],
                    index_pct=result["index_pct"], every_stock_pct=result["every_stock_pct"]))
            done.append(result)
        finished = conn.execute(select(db.correction_episodes)
                                .where(db.correction_episodes.c.finished_on.is_not(None))).mappings().all()
    scored = [(f["basket_pct"], f["index_pct"], f["every_stock_pct"]) for f in finished]
    scored += [] if record else [(d["basket_pct"], d["index_pct"], d["every_stock_pct"]) for d in done]
    for d in done:
        d.update(done=len(scored), beat_index=sum(b > i for b, i, _ in scored),
                 beat_every=sum(b > e for b, _, e in scored))
    return done


def open_basket(episode: cr.Episode, picks: list[dict[str, Any]], s: dict[str, Any]) -> None:
    with db.connection() as conn:
        episode_id = conn.execute(db.correction_episodes.insert().values(
            trigger_date=episode.trigger_date.date(), peak_date=episode.peak_date.date(),
            peak_level=episode.peak_level, trigger_level=episode.trigger_level, mode=s["mode"],
        )).inserted_primary_key[0]
        if picks:
            conn.execute(db.correction_trades.insert().values([
                {"episode_id": episode_id, "symbol": p["symbol"], "target": p["target"], "fall_pct": p["fall_pct"],
                 "closed": False} for p in picks]))


def recorded(trigger: date) -> bool:
    with db.connection() as conn:
        return conn.execute(select(db.correction_episodes.c.id)
                            .where(db.correction_episodes.c.trigger_date == trigger)).first() is not None


def run(cfg: Any, *, record: bool = True, session: date | None = None,
        data: tuple[pd.DataFrame, dict[str, pd.DataFrame]] | None = None) -> int:
    """One morning's work. Returns the number of messages sent.

    `record=False` (a run with alerts off) changes nothing, so the real run
    still finds and announces whatever this one saw.
    """
    s = settings(cfg)
    if not s["enabled"]:
        return 0
    db.init_db()
    if data is None:
        from src.data import nse
        data = load_data(s, session or nse.last_completed_session())
    idx, frames = data
    if idx is None or idx.empty:
        log.warning("No index history; the correction check is skipped")
        return 0

    sent = 0
    mode = str(s["mode"])
    exits = walk_open_trades(frames, s, cfg, record)
    if exits:
        sent += telegram.send(telegram.correction_exits(exits, mode), alert_type="correction_exit",
                              dedupe_key=f"correction_exit|{idx.index[-1].date()}", cfg=cfg)
    for result in finish_baskets(idx, frames, s, cfg, record):
        sent += telegram.send(telegram.correction_done(result, mode), alert_type="correction_done",
                              dedupe_key=f"correction_done|{result['trigger_date']}", cfg=cfg)

    episode = new_trigger(idx["close"], s)
    if episode is not None and not recorded(episode.trigger_date.date()):
        picks = picks_for(episode, frames, s)
        log.info("Correction triggered %s: %d stocks", episode.trigger_date.date(), len(picks))
        if record:
            open_basket(episode, picks, s)
        for n, body in enumerate(telegram.correction_buy(episode, picks, s, idx.index[-1].date())):
            sent += telegram.send(body, alert_type="correction_buy",
                                  dedupe_key=f"correction_buy|{episode.trigger_date.date()}|{n}", cfg=cfg)
    return sent
