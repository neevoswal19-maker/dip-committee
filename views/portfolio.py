"""Portfolio - holdings, the daily exit check, and the trade ledger."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import streamlit as st
from sqlalchemy import select

from src import charges as charge_model
from src import db
from src import portfolio as pf
from src.config import load_config
from src.data import prices
from src.data.provider import StockIdentity
from src.importers import groww
from src.strategy import exit as ex
from src.ui import theme

cfg = load_config()
db.init_db()
pf.backfill_transactions(cfg=cfg)

st.markdown("# Portfolio")
st.caption("What you hold, what the exit doctrine says about it today, and the tax clock.")


@st.cache_data(ttl=900, show_spinner=False)
def current_price(symbol: str) -> float | None:
    result = prices.get_price_history(StockIdentity(symbol), years=1)
    if result.usable and result.value is not None and not result.value.empty:
        return float(result.value["close"].iloc[-1])
    return None


def position_row(position_id: int) -> dict:
    with db.connection() as conn:
        row = conn.execute(
            select(db.positions).where(db.positions.c.id == position_id)
        ).first()
    return dict(row._mapping) if row else {}


tab_holdings, tab_record, tab_import, tab_ledger = st.tabs(
    ["Holdings", "Record a trade", "Import from broker", "Transactions"]
)


# ---------------------------------------------------------------- Holdings --

with tab_holdings:
    states = pf.open_positions(cfg=cfg)

    if not states:
        st.info(
            "No open positions. Record a trade in the next tab, or import a file "
            "from your broker."
        )
    else:
        rows = []
        signals_by_symbol: dict[str, list[ex.ExitSignal]] = {}
        total_invested = total_current = 0.0
        ltcg_days = int(cfg.get("exit.tax.ltcg_days", 366))

        for state in states:
            price = current_price(state.symbol)
            if price is None:
                continue

            record = position_row(state.position_id)
            peak = max(float(record.get("peak_price") or 0), price)

            if peak > float(record.get("peak_price") or 0):
                with db.connection() as conn:
                    conn.execute(
                        db.positions.update()
                        .where(db.positions.c.id == state.position_id)
                        .values(peak_price=peak, peak_date=date.today())
                    )

            position = ex.Position.from_state(
                state,
                stop_price=record.get("stop_price"),
                peak_price=peak,
                conviction=record.get("conviction"),
            )
            signals = ex.evaluate(position, price, cfg)
            signals_by_symbol[state.symbol] = signals
            decision = ex.decide(signals)

            total_invested += state.invested
            total_current += state.market_value(price)

            nearest = min(
                (lot for lot in state.lots if not lot.is_long_term(ltcg_days)),
                key=lambda l: l.trade_date,
                default=None,
            )

            rows.append({
                "Symbol": state.symbol,
                "Qty": state.quantity,
                "Avg cost": state.avg_cost,
                "Price": price,
                "Gain %": state.gain_pct(price),
                "Value": state.market_value(price),
                "P&L": state.unrealised(price),
                "Lots": len(state.lots),
                "To LTCG": nearest.days_to_ltcg(ltcg_days) if nearest else 0,
                "Action": decision.action,
            })

        if rows:
            pnl = total_current - total_invested
            summary = st.columns(5)
            summary[0].metric("Invested", theme.rupees(total_invested))
            summary[1].metric("Current value", theme.rupees(total_current))
            summary[2].metric(
                "Unrealised P&L", theme.rupees(pnl),
                theme.pct(pnl / total_invested * 100 if total_invested else 0, 1, True),
            )
            summary[3].metric("Positions", len(rows))
            summary[4].metric(
                "Needing action",
                sum(1 for r in rows if r["Action"] in ("EXIT", "TRIM")),
            )

            realised = pf.realised_summary()
            if realised["disposal_count"]:
                st.caption(
                    f"Realised so far: {theme.rupees(realised['total_gain'])} "
                    f"({theme.rupees(realised['short_term_gain'])} short-term, "
                    f"{theme.rupees(realised['long_term_gain'])} long-term) "
                    f"across {realised['disposal_count']} disposals."
                )

            actionable = [
                (symbol, ex.decide(sigs))
                for symbol, sigs in signals_by_symbol.items()
                if ex.decide(sigs).action in ("EXIT", "TRIM", "REVIEW")
            ]
            if actionable:
                st.markdown("## Signals")
                for symbol, decision in sorted(
                    actionable, key=lambda item: ex.ACTION_ORDER[item[1].action], reverse=True
                ):
                    colour = theme.STANCE_COLORS.get(decision.action, theme.COLORS["neutral"])
                    st.markdown(
                        f'<div class="card"><div style="display:flex;gap:0.6rem;align-items:center">'
                        f'<strong style="font-size:1.05rem">{symbol}</strong>'
                        f"{theme.pill(decision.action, colour)}"
                        f'<span class="label">{decision.rule.replace("_", " ")}</span></div>'
                        f'<div style="margin-top:0.4rem;font-size:0.9rem;line-height:1.55;color:#CBD5E1">'
                        f"{decision.message}</div></div>",
                        unsafe_allow_html=True,
                    )

            st.markdown("## Holdings")
            st.dataframe(
                pd.DataFrame(rows), use_container_width=True, hide_index=True,
                column_config={
                    "Avg cost": st.column_config.NumberColumn(
                        format="%.2f", help="Weighted average, including charges"
                    ),
                    "Price": st.column_config.NumberColumn(format="%.2f"),
                    "Gain %": st.column_config.NumberColumn(format="%.1f"),
                    "Value": st.column_config.NumberColumn(format="%.0f"),
                    "P&L": st.column_config.NumberColumn(format="%.0f"),
                    "Lots": st.column_config.NumberColumn(
                        help="Separate purchases, each with its own tax clock"
                    ),
                    "To LTCG": st.column_config.NumberColumn(
                        help="Days until the OLDEST remaining lot turns long-term. "
                             "FIFO sells those shares first."
                    ),
                },
            )

            st.markdown("## Lots and the tax clock")
            st.caption(
                "A staged entry buys the same position several times, so there is no single "
                "anniversary. Indian rules sell the oldest shares first, so the top lot in "
                "each table is the one a sale would dispose of."
            )

            for state in states:
                price = current_price(state.symbol)
                if price is None:
                    continue

                with st.expander(
                    f"{state.symbol}  -  {state.quantity:g} shares across "
                    f"{len(state.lots)} lot(s)  -  {theme.pct(state.gain_pct(price), 1, True)}"
                ):
                    lot_rows = [{
                        "Bought": lot.trade_date.isoformat(),
                        "Tranche": lot.tranche_label or "-",
                        "Qty": lot.remaining,
                        "Cost": lot.cost_per_share,
                        "Gain %": (price - lot.cost_per_share) / lot.cost_per_share * 100,
                        "Held": lot.days_held(),
                        "LTCG on": lot.ltcg_date(ltcg_days).isoformat(),
                        "Days to go": lot.days_to_ltcg(ltcg_days),
                        "Status": "long-term" if lot.is_long_term(ltcg_days) else "short-term",
                    } for lot in state.lots]

                    st.dataframe(
                        pd.DataFrame(lot_rows), use_container_width=True, hide_index=True,
                        column_config={
                            "Cost": st.column_config.NumberColumn(format="%.2f"),
                            "Gain %": st.column_config.NumberColumn(format="%.1f"),
                        },
                    )

                    for signal in signals_by_symbol.get(state.symbol, []):
                        kind = {"urgent": "fail", "warn": "warn", "info": "pass"}[signal.severity]
                        st.markdown(
                            f'<div class="reason reason-{kind}"><strong>{signal.action}</strong> '
                            f"&middot; {signal.message}</div>",
                            unsafe_allow_html=True,
                        )

                    if state.disposals:
                        st.markdown("**Already sold from this position**")
                        st.dataframe(
                            pd.DataFrame([{
                                "Sold": d.sell_date.isoformat(),
                                "Qty": d.quantity,
                                "Bought": d.buy_date.isoformat(),
                                "Cost": round(d.buy_cost_per_share, 2),
                                "Proceeds": round(d.sell_price_per_share, 2),
                                "Gain": d.gain,
                                "Treatment": "long-term" if d.is_long_term else "short-term",
                            } for d in state.disposals]),
                            use_container_width=True, hide_index=True,
                        )


# --------------------------------------------------------- Record a trade --

with tab_record:
    st.caption(
        "Record what you actually did at your broker. Nothing here places an order."
    )

    side = st.radio("Trade type", ["BUY", "SELL"], horizontal=True, key="side")
    open_symbols = [s.symbol for s in pf.open_positions(cfg=cfg)]

    left, right = st.columns([1, 1])

    with left:
        if side == "SELL":
            if not open_symbols:
                st.warning("Nothing held, so there is nothing to sell.")
                symbol = ""
            else:
                symbol = st.selectbox("Symbol", open_symbols)
        else:
            mode = st.radio(
                "Position", ["Add to an existing holding", "Start a new position"],
                horizontal=False, key="buy_mode",
                disabled=not open_symbols,
            )
            if mode == "Add to an existing holding" and open_symbols:
                symbol = st.selectbox("Symbol", open_symbols)
            else:
                symbol = st.text_input("Symbol", placeholder="INFY").strip().upper()

        quantity = st.number_input("Quantity", min_value=1, value=10, step=1)
        price = st.number_input("Price per share (Rs)", min_value=0.01, value=100.0, step=0.05)
        trade_date = st.date_input("Trade date", date.today(), max_value=date.today())

    with right:
        broker = st.selectbox(
            "Broker", ["groww", "zerodha", "upstox", "manual"],
            index=0,
            help="Sets the brokerage model used to prefill charges.",
        )
        tranche = st.selectbox(
            "Tranche",
            ["T1 (signal)", "T2 (further weakness)", "T3 (trend confirmed)", "trim", "exit", "-"],
            index=0 if side == "BUY" else 3,
        )

        estimated = charge_model.estimate(side, quantity, price, cfg=cfg, broker=broker)
        st.markdown(
            f'<div class="card card-tight"><div class="label">Estimated charges</div>'
            f'<div class="mono" style="font-size:1.1rem">{theme.rupees(estimated.total, 2)}</div>'
            f'<div style="font-size:0.7rem;color:#64748B">brokerage {estimated.brokerage:.2f} '
            f'&middot; STT {estimated.stt:.2f} &middot; GST {estimated.gst:.2f}'
            f'{" &middot; DP " + format(estimated.dp_charge, ".2f") if estimated.dp_charge else ""}'
            f'{" &middot; stamp " + format(estimated.stamp_duty, ".2f") if estimated.stamp_duty else ""}'
            f"</div></div>",
            unsafe_allow_html=True,
        )

        use_actual = st.checkbox(
            "Enter the actual charges from the contract note", value=False,
            help="The estimate is close but brokers round differently. "
                 "The contract note figure is the one that is true.",
        )
        actual_charges = (
            st.number_input("Total charges (Rs)", min_value=0.0,
                            value=float(estimated.total), step=0.01)
            if use_actual else None
        )

    if side == "BUY":
        conviction = st.slider("Conviction at entry", 0, 100, 70)
        recommendation = st.selectbox(
            "Recommendation", ["BALANCED", "AGGRESSIVE", "CONSERVATIVE"]
        )
        stop_price = st.number_input("Stop price (Rs, 0 for none)", min_value=0.0, value=0.0, step=0.05)
    else:
        conviction = recommendation = None
        stop_price = 0.0

    notes = st.text_input("Notes", placeholder="optional")

    total = quantity * price
    net = charge_model.net_amount(
        side, quantity, price,
        actual_charges if actual_charges is not None else estimated.total,
    )
    st.markdown(
        f"**{side} {quantity:g} {symbol or '...'} at {theme.rupees(price, 2)}** "
        f"&mdash; turnover {theme.rupees(total)}, "
        f"{'costing' if side == 'BUY' else 'returning'} {theme.rupees(net)} after charges.",
        unsafe_allow_html=True,
    )

    if st.button(f"Record {side.lower()}", type="primary", disabled=not symbol):
        try:
            charges_payload = (
                {"total": actual_charges, "other": actual_charges}
                if actual_charges is not None
                else estimated.to_dict()
            )

            position_id = pf.find_open_position(symbol)
            if side == "BUY" and position_id is None:
                doctrine = ex.build_doctrine(
                    symbol, float(price), cfg,
                    stop_price=float(stop_price) or None, entry_date=trade_date,
                )
                position_id = pf.create_position(
                    symbol,
                    conviction=float(conviction) if conviction is not None else None,
                    recommendation=recommendation,
                    stop_price=float(stop_price) or None,
                    exit_doctrine=doctrine.to_dict(),
                )

            _, state = pf.record_trade(
                symbol, side, trade_date, float(quantity), float(price),
                position_id=position_id, charges=charges_payload, broker=broker,
                tranche_label=tranche.split(" ")[0] if tranche != "-" else None,
                notes=notes or None, cfg=cfg,
            )

            if state.is_open:
                st.success(
                    f"Recorded. {symbol}: {state.quantity:g} shares at an average cost of "
                    f"{theme.rupees(state.avg_cost, 2)}."
                )
            else:
                st.success(
                    f"Recorded. {symbol} is now closed, realising "
                    f"{theme.rupees(state.realised_gain)}."
                )
            st.rerun()

        except pf.LedgerError as exc:
            st.error(str(exc))
        except Exception as exc:
            st.error(f"Could not record the trade: {exc}")


# ------------------------------------------------------------------ Import --

with tab_import:
    st.caption(
        "Upload a tradebook or order history from your broker. Nothing is written until "
        "you confirm, and importing the same file twice adds nothing."
    )

    broker_choice = st.selectbox(
        "Broker", ["groww", "zerodha", "upstox", "manual"], index=0, key="import_broker"
    )
    uploaded = st.file_uploader(
        "Trade file", type=["csv", "xlsx", "xls"],
        help="Groww: Orders or Transactions, exported as CSV or XLSX.",
    )

    if uploaded is not None:
        try:
            frame = groww.read_file(uploaded.getvalue(), uploaded.name)
        except Exception as exc:
            st.error(f"Could not read the file: {exc}")
            frame = None

        if frame is not None and not frame.empty:
            st.markdown(f"**{len(frame)} rows**, columns: `{', '.join(str(c) for c in frame.columns)}`")

            detected = groww.detect_mapping(frame)
            st.markdown("#### Column mapping")
            st.caption("Auto-detected. Correct anything that is wrong before importing.")

            options = ["(none)"] + [str(c) for c in frame.columns]
            mapping: dict[str, str | None] = {}
            map_cols = st.columns(4)

            for i, field_name in enumerate(
                ["symbol", "side", "quantity", "price", "trade_date", "order_id", "charges", "exchange"]
            ):
                guess = detected.get(field_name)
                index = options.index(str(guess)) if guess and str(guess) in options else 0
                required = field_name in groww.REQUIRED
                chosen = map_cols[i % 4].selectbox(
                    f"{field_name.replace('_', ' ')}{' *' if required else ''}",
                    options, index=index, key=f"map_{field_name}",
                )
                mapping[field_name] = None if chosen == "(none)" else chosen

            missing = [f for f in groww.REQUIRED if not mapping.get(f)]
            if missing:
                st.error(f"These are required before importing: {', '.join(missing)}")
            else:
                with st.spinner("Checking rows..."):
                    preview = groww.build_preview(
                        frame, mapping=mapping, broker=broker_choice,
                        symbol_overrides=st.session_state.get("symbol_overrides", {}),
                    )

                counts = st.columns(3)
                counts[0].metric("Ready to import", len(preview.ready))
                counts[1].metric("Already recorded", len(preview.duplicates))
                counts[2].metric("Need attention", len(preview.problems))

                if preview.unresolved_names:
                    st.markdown("#### Unmatched names")
                    st.caption(
                        "These could not be matched to an NSE symbol confidently. Map them "
                        "by hand - a wrong guess would attach trades to the wrong company."
                    )
                    overrides = dict(st.session_state.get("symbol_overrides", {}))
                    for name in preview.unresolved_names[:10]:
                        entered = st.text_input(
                            f"'{name}' is", value=overrides.get(name, ""),
                            placeholder="NSE symbol", key=f"override_{name}",
                        ).strip().upper()
                        if entered:
                            overrides[name] = entered
                    if overrides != st.session_state.get("symbol_overrides", {}):
                        st.session_state["symbol_overrides"] = overrides
                        st.rerun()

                st.dataframe(
                    pd.DataFrame([r.to_display() for r in preview.rows]),
                    use_container_width=True, hide_index=True,
                )

                if preview.ready and st.button(
                    f"Import {len(preview.ready)} trades", type="primary"
                ):
                    with st.spinner("Importing..."):
                        result = groww.commit(preview, broker=broker_choice, cfg=cfg)

                    st.success(
                        f"Imported {result['imported']} trades. "
                        f"Skipped {result['skipped_duplicates']} already recorded and "
                        f"{result['skipped_problems']} needing attention."
                    )
                    if result["failures"]:
                        st.warning("Some rows failed:")
                        st.dataframe(pd.DataFrame(result["failures"]), hide_index=True)
                    st.session_state.pop("symbol_overrides", None)


# ------------------------------------------------------------- Transactions --

with tab_ledger:
    st.caption(
        "Every buy and sell. Holdings, average cost and realised gains are all "
        "derived from these rows, so correcting a mistake here corrects everything."
    )

    transactions = pf.all_transactions(limit=500)

    if not transactions:
        st.info("No transactions recorded yet.")
    else:
        st.dataframe(
            pd.DataFrame([{
                "Date": t["trade_date"].isoformat() if t["trade_date"] else None,
                "Symbol": t["symbol"],
                "Side": t["side"],
                "Qty": t["quantity"],
                "Price": t["price"],
                "Charges": t["total_charges"],
                "Net": t["net_amount"],
                "Tranche": t["tranche_label"] or "-",
                "Broker": t["broker"],
                "Source": t["source"],
                "ID": t["id"],
            } for t in transactions]),
            use_container_width=True, hide_index=True,
            column_config={
                "Price": st.column_config.NumberColumn(format="%.2f"),
                "Charges": st.column_config.NumberColumn(format="%.2f"),
                "Net": st.column_config.NumberColumn(format="%.2f"),
            },
        )

        st.markdown("#### Remove a transaction")
        st.caption("For a mistyped entry. The position recalculates immediately.")
        remove_cols = st.columns([1, 3])
        transaction_id = remove_cols[0].number_input(
            "Transaction ID", min_value=0, value=0, step=1, label_visibility="collapsed"
        )
        if remove_cols[1].button("Delete", disabled=transaction_id <= 0):
            try:
                pf.delete_transaction(int(transaction_id), cfg=cfg)
                st.success(f"Deleted transaction {transaction_id} and recalculated.")
                st.rerun()
            except pf.LedgerError as exc:
                st.error(str(exc))
