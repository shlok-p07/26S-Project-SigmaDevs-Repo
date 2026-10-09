import logging
logger = logging.getLogger(__name__)

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from modules.nav import SideBarLinks
from modules import quant_ui as ui

st.set_page_config(layout='wide', page_title="Trading Logs · PortIQ")

SideBarLinks()

ui.setup_page("Trading Logs",
              "Where trading volume goes, whether the desk is a net buyer or seller, and the full trade record.")

rows, err = ui.api_get("/trades/")
if err:
    st.error(f"Could not load trades. {err}")
    st.stop()
trades = ui.to_frame(rows, ("price", "quantity"))
trades["trade_date"] = pd.to_datetime(trades.trade_date, utc=True).dt.tz_convert(None)
trades["ticker"] = trades.ticker.fillna("Unknown")
trades["notional"] = trades.price * trades.quantity
trades["signed"] = trades.notional.where(trades.trade_type == "BUY", -trades.notional)

# ---- Filters ------------------------------------------------------------------

f1, f2, f3 = st.columns([3, 2, 4])
lo, hi = trades.trade_date.min().date(), trades.trade_date.max().date()
dates = f1.date_input("Date range", (lo, hi), min_value=lo, max_value=hi)
sides = f2.pills("Side", ["BUY", "SELL"], default=["BUY", "SELL"], selection_mode="multi") or ["BUY", "SELL"]
tickers = f3.multiselect("Tickers", sorted(trades.ticker.unique()), placeholder="All tickers")

start, end = (dates if isinstance(dates, tuple) and len(dates) == 2 else (lo, hi))
view = trades[(trades.trade_date.dt.date >= start) & (trades.trade_date.dt.date <= end)
              & trades.trade_type.isin(sides)]
if tickers:
    view = view[view.ticker.isin(tickers)]

# ---- KPIs ---------------------------------------------------------------------

if view.empty:
    st.info("No trades match these filters.")
else:
    buys, sells = view[view.trade_type == "BUY"], view[view.trade_type == "SELL"]
    net = view.signed.sum()
    largest = view.loc[view.notional.idxmax()]
    ui.kpi_row([
        ui.kpi("Trades", f"{len(view)}", f"{len(buys)} buys · {len(sells)} sells"),
        ui.kpi("Gross notional", ui.money(view.notional.sum()), "total traded value"),
        ui.kpi("Net flow", ui.money(net, sign=True),
               note="net buyer" if net >= 0 else "net seller"),
        ui.kpi("Average ticket", ui.money(view.notional.mean()), "per trade"),
        ui.kpi("Largest trade", ui.money(largest.notional),
               f"{largest.trade_type} {largest.ticker} · {largest.trade_date:%b %d, %Y}"),
    ])

    # ---- Charts -------------------------------------------------------------

    left, right = st.columns([7, 5], gap="medium")
    with left:
        with st.container(border=True):
            ui.card_header("Monthly trading volume", "Notional traded per month, buys and sells stacked")
            monthly = (view.assign(month=view.trade_date.dt.to_period("M").dt.to_timestamp())
                       .pivot_table(index="month", columns="trade_type", values="notional",
                                    aggfunc="sum", fill_value=0)
                       .reindex(columns=["BUY", "SELL"], fill_value=0))
            fig = go.Figure()
            for side, color in (("BUY", ui.SERIES_1), ("SELL", ui.SERIES_2)):
                fig.add_trace(go.Bar(
                    x=monthly.index, y=monthly[side], name=side.capitalize() + "s",
                    marker=dict(color=color, line=dict(color=ui.SURFACE, width=2)),
                    hovertemplate=f"%{{x|%b %Y}} · {side.lower()}s $%{{y:,.0f}}<extra></extra>",
                ))
            fig.update_layout(barmode="stack", legend_traceorder="normal")
            fig.update_yaxes(tickprefix="$", tickformat="~s")
            fig.update_xaxes(dtick="M1", tickformat="%b<br>%Y")
            fig = ui.style_fig(fig, height=300)
            fig.update_layout(bargap=0.35)
            ui.show(fig)

    with right:
        with st.container(border=True):
            ui.card_header("Volume by ticker", "Top 8 by notional; buys and sells stacked")
            by_ticker = (view.pivot_table(index="ticker", columns="trade_type", values="notional",
                                          aggfunc="sum", fill_value=0)
                         .reindex(columns=["BUY", "SELL"], fill_value=0))
            by_ticker = by_ticker.assign(total=by_ticker.sum(axis=1)).nlargest(8, "total").iloc[::-1]
            fig = go.Figure()
            for side, color in (("BUY", ui.SERIES_1), ("SELL", ui.SERIES_2)):
                fig.add_trace(go.Bar(
                    y=by_ticker.index, x=by_ticker[side], orientation="h", name=side.capitalize() + "s",
                    marker=dict(color=color, line=dict(color=ui.SURFACE, width=2)),
                    hovertemplate=f"%{{y}} · {side.lower()}s $%{{x:,.0f}}<extra></extra>",
                ))
            fig.update_layout(barmode="stack", legend_traceorder="normal")
            fig.update_xaxes(tickprefix="$", tickformat="~s", showgrid=True, gridcolor=ui.GRID)
            fig = ui.style_fig(fig, height=300)
            fig.update_layout(bargap=0.35)
            ui.show(fig)

    # ---- Trade record -------------------------------------------------------

    with st.container(border=True):
        ui.card_header("Trade record", f"{len(view)} trades, newest first")
        cols = ["trade_date", "trade_id", "trade_type", "ticker", "asset_name", "quantity", "price", "notional"]
        st.dataframe(view.sort_values("trade_date", ascending=False)[cols], hide_index=True,
                     use_container_width=True, height=340, column_config={
                         "trade_date": st.column_config.DatetimeColumn("Date", format="MMM D, YYYY  h:mm a"),
                         "trade_id": st.column_config.NumberColumn("ID", format="%d", width=70),
                         "trade_type": st.column_config.TextColumn("Side", width=60),
                         "ticker": st.column_config.TextColumn("Ticker", width=70),
                         "asset_name": "Asset",
                         "quantity": st.column_config.NumberColumn("Quantity", format="%,.0f"),
                         "price": st.column_config.NumberColumn("Price ($)", format="%,.2f"),
                         "notional": st.column_config.NumberColumn("Notional ($)", format=ui.MONEY),
                     })
        st.download_button("Download CSV", view[cols].to_csv(index=False), "portiq_trades.csv", mime="text/csv")

# ---- Record, edit, delete -----------------------------------------------------

assets, _ = ui.api_get("/quant/assets")
asset_labels = {a["asset_id"]: f'{a["ticker"]} · {a["asset_name"]} · #{a["asset_id"]}' for a in assets or []}
last_close = {a["asset_id"]: float(a["last_close"]) for a in assets or [] if a.get("last_close")}
trade_labels = {r.trade_id: f"#{r.trade_id} · {r.trade_type} {r.quantity:,.0f} {r.ticker} @ ${r.price:,.2f} · "
                            f"{r.trade_date:%b %d, %Y}"
                for r in trades.sort_values("trade_date", ascending=False).itertuples()}

with st.container(border=True):
    ui.card_header("Manage trades", "New trades get the next available ID automatically")
    ui.show_flash()
    new_tab, edit_tab, delete_tab = st.tabs(["Record a trade", "Edit a trade", "Delete a trade"])

    with new_tab:
        source = st.segmented_control("Trade", ["Existing assets", "Any U.S. stock or ETF"], default="Existing assets",
                                      key=ui.widget_key("asset_source")) or "Existing assets"
        new_ticker, ready = None, True
        if source == "Existing assets":
            asset = st.selectbox("Asset", list(asset_labels), format_func=asset_labels.get,
                                 key=ui.widget_key("new_asset"))
        else:
            q = st.text_input("Search by ticker or company name", placeholder="e.g. KO, Berkshire, Costco",
                              key=ui.widget_key("asset_search"))
            found, _ = ui.api_get("/quant/securities/search", q=q) if q else ([], None)
            if not found:
                st.caption("Type to search 11,000+ U.S.-listed stocks and ETFs.")
                ready, asset = False, None
            if found:
                found_labels = {f["ticker"]: f"{f['ticker']} · {f['name'][:60]} · {f['exchange']}" for f in found}
                new_ticker = st.selectbox("Security", list(found_labels), format_func=found_labels.get,
                                          key=ui.widget_key("asset_found"))
                match = next(f for f in found if f["ticker"] == new_ticker)
                asset = None
                if match.get("close_value"):
                    last_close[None] = float(match["close_value"])
        if ready:
            with st.form("new_trade", clear_on_submit=True, border=False):
                b, c, d = st.columns(3)
                side = b.segmented_control("Side", ["BUY", "SELL"], default="BUY")
                qty = c.number_input("Quantity", min_value=1.0, value=10.0, step=1.0)
                price = d.number_input("Price ($)", min_value=0.01, value=last_close.get(asset, 100.0), step=0.01,
                                       help="Defaults to the latest real close")
                if st.form_submit_button("Record trade", type="primary"):
                    if new_ticker:  # make the security tradable first
                        ok, created = ui.api_write("POST", "/quant/assets", {"ticker": new_ticker})
                        if not ok:
                            st.error(created)
                            st.stop()
                        asset = created["asset_id"]
                    ui.write_and_refresh("POST", "/trades/", {
                        "trade_type": side or "BUY", "quantity": qty, "price": price, "trade_asset": int(asset)},
                        success=f"Recorded trade #{{trade_id}}: {side or 'BUY'} {qty:,.0f} for "
                                f"{ui.money(qty * price, compact=False).replace('$', chr(92) + '$')}.")

    with edit_tab:
        trade_id = st.selectbox("Trade", list(trade_labels), format_func=trade_labels.get, key=ui.widget_key("edit_pick"))
        t = trades.set_index("trade_id").loc[trade_id]
        with st.form("edit_trade", border=False):
            a, b, c, d = st.columns([4, 2, 2, 2])
            asset_ids = list(asset_labels)
            asset = a.selectbox("Asset", asset_ids, format_func=asset_labels.get,
                                index=asset_ids.index(t.trade_asset) if t.trade_asset in asset_ids else 0)
            side = b.segmented_control("Side", ["BUY", "SELL"], default=t.trade_type)
            qty = c.number_input("Quantity", min_value=1.0, value=float(t.quantity), step=1.0)
            price = d.number_input("Price ($)", min_value=0.01, value=float(t.price), step=0.01)
            if st.form_submit_button("Save changes", type="primary"):
                ui.write_and_refresh("PUT", f"/trades/{trade_id}", {
                    "trade_type": side or t.trade_type, "quantity": qty, "price": price,
                    "trade_asset": int(asset)}, success=f"Trade #{trade_id} updated.")

    with delete_tab:
        trade_id = st.selectbox("Trade", list(trade_labels), format_func=trade_labels.get, key=ui.widget_key("delete_pick"))
        confirm = st.checkbox("I understand this permanently deletes the trade")
        if st.button("Delete trade", disabled=not confirm):
            ui.write_and_refresh("DELETE", f"/trades/{trade_id}", success=f"Trade #{trade_id} deleted.")
