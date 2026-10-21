"""The intraday ORB + VWAP/20-EMA pullback rules, on synthetic 5-minute sessions.

Day 1 is flat at 100 (it only warms the 20 EMA and RSI up). Day 2 is the
textbook long: a heavy dip in the opening range, a breakout at 9:30 on rising
volume, a two-candle pullback that kisses the 20 EMA on light volume, then a
bullish trigger. Each test changes one thing.
"""

from __future__ import annotations

from datetime import date, time

import pandas as pd
import pytest

from src.config import load_config
from src.intraday import orb

CFG = load_config()
DAY1, DAY2 = "2026-10-15", "2026-10-16"
# The mechanics tests pin the targets; the live setting is checked on its own below.
PARAMS = {**orb.params_for("NIFTYBEES", CFG), "partial_r": 1.5, "final_r": 3.0}


def _times():
    start = pd.Timestamp(f"{DAY1} 09:15")
    return [(start + pd.Timedelta(minutes=5 * i)).time() for i in range(75)]   # 9:15 .. 15:25


def _day(day: str, rows: dict[time, tuple], fill: tuple) -> pd.DataFrame:
    """A full session: `rows` at their times, `fill` (o, h, l, c, v) at every other time."""
    out = [(pd.Timestamp(f"{day} {t:%H:%M}"), *rows.get(t, fill)) for t in _times()]
    return pd.DataFrame(out, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts")


T = lambda h, m: time(h, m)  # noqa: E731

ORB = {T(9, 15): (99.8, 99.9, 99.6, 99.7, 4000.0), T(9, 20): (99.8, 99.9, 99.6, 99.7, 4000.0),
       T(9, 25): (99.8, 99.9, 99.6, 99.7, 4000.0)}
BREAKOUT = {T(9, 30): (99.9, 100.4, 99.9, 100.3, 2500.0)}
PULLBACK = {T(9, 35): (100.3, 100.35, 100.05, 100.1, 800.0)}
TRIGGER = {T(9, 40): (100.1, 100.35, 100.08, 100.3, 900.0)}
TO_TARGETS = {T(9, 45): (100.3, 101.3, 100.25, 101.2, 1000.0), T(9, 50): (101.2, 102.1, 101.1, 102.0, 1000.0)}


def _bars(day2_rows: dict, fill=(102.0, 102.0, 102.0, 102.0, 1000.0)) -> pd.DataFrame:
    flat = _day(DAY1, {}, (100.0, 100.0, 100.0, 100.0, 1000.0))
    return pd.concat([flat, _day(DAY2, day2_rows, fill)])


def textbook(**extra) -> pd.DataFrame:
    rows = {**ORB, **BREAKOUT, **PULLBACK, **TRIGGER, **TO_TARGETS}
    rows.update(extra.get("rows", {}))
    return _bars(rows, extra.get("fill", (102.0, 102.0, 102.0, 102.0, 1000.0)))


def run(bars: pd.DataFrame, params=PARAMS):
    return orb.session_events(orb.with_indicators(bars), date(2026, 10, 16), "NIFTYBEES", params)


def kinds(events):
    return [e.kind for e in events]


def mirror(bars: pd.DataFrame) -> pd.DataFrame:
    """The same session upside down, around 100: a short setup."""
    return pd.DataFrame({"open": 200 - bars["open"], "high": 200 - bars["low"], "low": 200 - bars["high"],
                         "close": 200 - bars["close"], "volume": bars["volume"]}, index=bars.index)


# --- Entries --------------------------------------------------------------------------


def test_the_textbook_long_arms_enters_takes_half_and_exits_at_3R():
    events, trade = run(textbook())
    assert kinds(events) == ["armed", "entry", "partial", "exit"]
    armed, entry, partial, exit_ = events
    assert armed.when == pd.Timestamp(f"{DAY2} 09:35") and armed.side == "LONG"
    assert entry.when == pd.Timestamp(f"{DAY2} 09:45")             # the 9:40 candle's close
    assert entry.detail["stop"] < entry.price < entry.detail["target_partial"] < entry.detail["target_final"]
    assert partial.detail["units"] == trade.units // 2
    assert partial.detail["new_stop"] == pytest.approx(trade.entry, abs=0.01)
    assert exit_.detail["reason"] == "target_3R" and trade.closed


def test_the_mirror_image_is_a_short():
    events, trade = run(mirror(textbook()))
    assert kinds(events) == ["armed", "entry", "partial", "exit"]
    assert {e.side for e in events} == {"SHORT"}
    entry = events[1]
    assert entry.detail["stop"] > entry.price > entry.detail["target_partial"] > entry.detail["target_final"]
    assert trade.exit_reason == "target_3R"


def test_the_stop_is_beyond_the_pullback_low_or_vwap_whichever_is_further():
    events, trade = run(textbook())
    f = orb.with_indicators(textbook())
    vwap = float(f.loc[pd.Timestamp(f"{DAY2} 09:40"), "vwap"])
    assert trade.stop == pytest.approx(min(100.05, vwap * 0.999))


def test_size_is_5000_rupees_of_risk_capped_at_7_5_lakh_of_exposure():
    _, trade = run(textbook())
    assert trade.units * trade.risk_per_unit <= 5000 + 1e-6
    assert trade.units * trade.entry <= 750_000 + 1e-6
    capped = int(750_000 // trade.entry)
    assert trade.units == min(int(5000 // trade.risk_per_unit), capped)


def test_a_wide_opening_range_skips_the_day():
    wide = {T(9, 15): (99.8, 99.9, 98.9, 99.7, 4000.0)}             # 1.0% > 0.85%
    events, trade = run(textbook(rows=wide))
    assert kinds(events) == ["skipped"] and trade is None


def test_a_breakout_on_weak_volume_does_not_arm():
    events, trade = run(textbook(rows={T(9, 30): (99.9, 100.4, 99.9, 100.3, 1500.0)}))
    assert trade is None
    assert all(e.when != pd.Timestamp(f"{DAY2} 09:35") for e in events)


def test_a_heavy_pullback_cancels_the_setup():
    events, trade = run(textbook(rows={T(9, 35): (100.3, 100.35, 100.05, 100.1, 3000.0)}))
    assert "cancelled" in kinds(events) and trade is None


def test_rsi_falling_through_50_cancels_the_setup():
    events, trade = run(textbook(rows={T(9, 35): (100.3, 100.35, 99.7, 99.75, 800.0)}))
    assert events[1].kind == "cancelled" and "RSI" in events[1].detail["reason"]
    assert trade is None


def test_no_trigger_within_six_candles_expires():
    drift = (100.3, 100.32, 100.05, 100.25, 800.0)                  # touches, never bullish
    rows = {T(9, 35): drift, T(9, 40): drift, T(9, 45): drift, T(9, 50): drift, T(9, 55): drift,
            T(10, 0): drift}
    events, trade = run(_bars({**ORB, **BREAKOUT, **rows}, fill=drift))
    assert "expired" in kinds(events) and trade is None
    assert next(e for e in events if e.kind == "expired").when == pd.Timestamp(f"{DAY2} 10:05")


# --- Exits ----------------------------------------------------------------------------


def test_the_hard_stop():
    events, trade = run(textbook(rows={T(9, 45): (100.3, 100.4, 99.6, 99.8, 1000.0)}))
    assert trade.exit_reason == "stop"
    assert events[-1].price == pytest.approx(trade.stop * (1 - 0.0002), abs=0.01)


def test_after_the_partial_a_close_below_the_20_ema_exits_the_rest():
    hold = (101.2, 101.3, 101.1, 101.2, 1000.0)
    rows = {T(9, 45): (100.3, 101.3, 100.25, 101.2, 1000.0)}
    t = pd.Timestamp(f"{DAY2} 09:50")
    for _ in range(12):
        rows[t.time()] = hold
        t += pd.Timedelta(minutes=5)
    rows[t.time()] = (101.0, 101.05, 100.6, 100.65, 1000.0)
    events, trade = run(_bars({**ORB, **BREAKOUT, **PULLBACK, **TRIGGER, **rows}, fill=(100.65,) * 4 + (1000.0,)))
    assert kinds(events) == ["armed", "entry", "partial", "exit"]
    assert trade.exit_reason == "trail_ema20"


def test_a_trade_still_open_is_squared_off_at_15_10():
    sideways = (100.5, 100.6, 100.4, 100.5, 1000.0)
    events, trade = run(_bars({**ORB, **BREAKOUT, **PULLBACK, **TRIGGER}, fill=sideways))
    assert trade.exit_reason == "time_stop_15:10"
    assert events[-1].when == pd.Timestamp(f"{DAY2} 15:10")


# --- The live job sees what a backtest sees -------------------------------------------


def test_adding_later_bars_never_changes_an_earlier_event():
    full = textbook()
    cut = full[full.index <= pd.Timestamp(f"{DAY2} 09:45")]          # the bar that closes at 9:50
    early, _ = run(cut)
    late, _ = run(full)
    assert [e.to_dict() for e in early] == [e.to_dict() for e in late if e.when <= pd.Timestamp(f"{DAY2} 09:50")]


def test_vwap_restarts_each_session():
    f = orb.with_indicators(textbook())
    first = f.loc[pd.Timestamp(f"{DAY2} 09:15")]
    assert first["vwap"] == pytest.approx((first["high"] + first["low"] + first["close"]) / 3)


# --- Costs, results and the verdict ---------------------------------------------------


def test_intraday_costs():
    costs = CFG.get("intraday.costs")
    sell = orb.leg_cost("SELL", 1000, 100.0, costs)
    buy = orb.leg_cost("BUY", 1000, 100.0, costs)
    service = 20.0 + 2.97 + 0.1
    assert sell == pytest.approx(service * 1.18 + 25.0, abs=0.01)    # STT 0.025% on the sell
    assert buy == pytest.approx(service * 1.18 + 3.0, abs=0.01)      # stamp 0.003% on the buy


def test_a_winning_trade_nets_its_fills_less_charges():
    _, trade = run(textbook())
    r = orb.result(trade, CFG.get("intraday.costs"))
    assert r["closed"] and r["net"] > 0 and r["gross"] > r["net"]
    assert 2.0 < r["r"] < 2.3          # half at 1.5R, half at 3R, less costs and slippage


def test_the_verdict_waits_for_40_trades_then_applies_the_bar():
    win = {"closed": True, "net": 6000.0, "r": 1.2, "side": "LONG"}
    loss = {"closed": True, "net": -5000.0, "r": -1.0, "side": "SHORT"}
    assert orb.judge(orb.scorecard([win] * 10, 500000))[0] is None
    good = orb.scorecard([win, win, loss] * 14, 500000)
    assert orb.judge(good)[0] is True
    bad = orb.scorecard([win, loss, loss] * 14, 500000)
    verdict, why = orb.judge(bad)
    assert verdict is False and any("profit factor" in w for w in why)


# --- Kept apart from the dip strategy -------------------------------------------------


def test_replies_to_intraday_messages_are_never_recorded_as_trades():
    from src.alerts import telegram_inbox

    reply = telegram_inbox.parse("NIFTYBEES 100", "INTRADAY · PAPER BUY: NIFTYBEES\nEntry 290.50")
    assert isinstance(reply, telegram_inbox.Problem)
    assert "paper" in reply.reply.lower()


# --- Telegram and the live job ----------------------------------------------------------


def test_the_entry_message_carries_the_label_levels_and_size():
    from src.alerts import telegram

    events, _ = run(textbook())
    body = telegram.intraday_event(events[1], "paper", "Nifty 50")
    assert body.startswith("<b>INTRADAY · PAPER BUY: NIFTYBEES</b> (Nifty 50)")
    for part in ("Stop Rs", "R Rs", "exit the rest", "units", "margin (MIS)", "15:10", "Paper trade"):
        assert part in body
    live = telegram.intraday_event(events[1], "live")
    assert live.startswith("<b>INTRADAY BUY: NIFTYBEES</b>") and "Paper trade" not in live


def test_skipped_days_are_not_announced():
    from src.alerts import telegram

    events, _ = run(textbook(rows={T(9, 15): (99.8, 99.9, 98.9, 99.7, 4000.0)}))
    assert telegram.intraday_event(events[0], "paper") is None


def test_the_live_job_wakes_20_seconds_after_each_candle():
    from datetime import datetime

    from jobs.intraday_live import next_wake

    assert next_wake(datetime(2026, 10, 19, 9, 31, 5)) == datetime(2026, 10, 19, 9, 35, 20)
    assert next_wake(datetime(2026, 10, 19, 9, 35, 0)) == datetime(2026, 10, 19, 9, 40, 20)


def test_only_one_live_session_runs_a_day(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from jobs.intraday_live import claim_session
    from src import db

    monkeypatch.setattr(db, "_engine", create_engine(f"sqlite:///{tmp_path/'i.db'}"))
    db.init_db()
    assert claim_session(date(2026, 10, 19), "paper") is True
    assert claim_session(date(2026, 10, 19), "paper") is False
    assert claim_session(date(2026, 10, 20), "paper") is True


def test_a_saved_trade_is_updated_not_duplicated(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, select

    from jobs.intraday_live import save_trade
    from src import db

    monkeypatch.setattr(db, "_engine", create_engine(f"sqlite:///{tmp_path/'t.db'}"))
    db.init_db()
    _, trade = run(textbook())
    costs = CFG.get("intraday.costs")
    save_trade(trade, "paper", costs)
    save_trade(trade, "paper", costs)
    with db.connection() as conn:
        rows = conn.execute(select(db.intraday_trades)).fetchall()
    assert len(rows) == 1 and rows[0].exit_reason == "target_3R" and rows[0].closed


def test_the_targets_are_settings():
    from src.alerts import telegram

    params = {**PARAMS, "partial_r": 1.0, "final_r": None}
    events, trade = run(textbook(), params)
    entry, partial = events[1], events[2]
    assert entry.detail["target_partial"] == pytest.approx(trade.entry + trade.risk_per_unit, abs=0.01)
    assert entry.detail["target_final"] is None
    assert partial.price == pytest.approx(trade.entry + trade.risk_per_unit, abs=0.01)
    assert trade.exit_reason == "time_stop_15:10"            # no final target: the rest only trails
    assert "No fixed final target" in telegram.intraday_event(entry, "paper")

    events, trade = run(textbook(), {**PARAMS, "partial_r": 1.5, "final_r": 2.5})
    assert trade.exit_reason == "target_2.5R"
    assert "2.5R target reached" in telegram.intraday_event(events[-1], "paper")


def test_the_live_targets_are_2R_then_2_5R():
    """Chosen on 2026-10-18 by the grid rule: beat 1.5R / 3R in both halves of 60 sessions."""
    live = orb.params_for("NIFTYBEES", CFG)
    assert (live["partial_r"], live["final_r"]) == (2.0, 2.5)


def test_the_run_always_ends_before_githubs_six_hour_limit():
    from datetime import datetime

    from jobs.intraday_live import deadline_for

    # 9:25 start: the full session, to 15:12.
    assert deadline_for(datetime(2026, 10, 22, 9, 25), time(15, 12), 345) == datetime(2026, 10, 22, 15, 12)
    # 9:12 start (the old timer): it stops itself at 14:57 and still sends the wrap-up,
    # instead of being killed at 15:10 with nothing sent.
    assert deadline_for(datetime(2026, 10, 22, 9, 12), time(15, 12), 345) == datetime(2026, 10, 22, 14, 57)
