import logging
logger = logging.getLogger(__name__)

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from modules.nav import SideBarLinks
from modules import quant_ui as ui

st.set_page_config(layout='wide', page_title="Stock Lookup · PortIQ")

SideBarLinks()

ui.setup_page("Stock Lookup & Backtest",
              "Any U.S. stock or ETF: how it has performed, and how each strategy would have traded it.")

# ---- Search ---------------------------------------------------------------------

c1, c2 = st.columns([2, 3])
query = c1.text_input("Search by ticker or company name", value=st.session_state.get("lookup_ticker", "AAPL"),
                      placeholder="e.g. KO, Berkshire, semiconductor ETF")
results, _ = ui.api_get("/quant/securities/search", q=query) if query else ([], None)
if not results:
    st.info("No listed U.S. security matches that search.")
    st.stop()
labels = {r["ticker"]: f"{r['ticker']} · {r['name'][:60]} · {r['exchange']}{' · ETF' if r['is_etf'] else ''}"
          for r in results}
ticker = c2.selectbox("Security", list(labels), format_func=labels.get)
st.session_state["lookup_ticker"] = ticker

detail, err = ui.api_get(f"/quant/securities/{ticker}")
if err:
    st.error(err)
    st.stop()
sec, snap = detail["security"], detail.get("snapshot") or {}
px = pd.DataFrame(detail["prices"])
px["date"] = pd.to_datetime(px.date)

st.markdown(ui.badge("ETF" if sec.get("is_etf") else "Stock", "good") +
            f'<span class="qt-card-sub">&nbsp; {sec.get("name", ticker)} · {sec.get("exchange", "")}</span>',
            unsafe_allow_html=True)

# ---- KPIs -------------------------------------------------------------------

year = px.tail(253).reset_index(drop=True)
stats = ui.perf_stats(year.adj_close, year.spy if year.spy.notna().all() else None)
last, prev = px.close.iloc[-1], px.close.iloc[-2] if len(px) > 1 else px.close.iloc[-1]
hi, lo = year.close.max(), year.close.min()
cards = [
    ui.kpi("Last close", f"${last:,.2f}", delta=ui.pct(last / prev - 1, 2, sign=True), delta_good=last >= prev,
           note=px.date.iloc[-1].strftime("%b %d, %Y")),
    ui.kpi("1-year return", ui.pct(stats["return"], 1, sign=True),
           delta=f"{ui.pct(stats['excess'], 1, sign=True)} vs S&P 500" if "excess" in stats else None,
           delta_good=stats.get("excess", 0) >= 0),
    ui.kpi("Volatility", ui.pct(stats["vol"]), "annualized, 1 year"),
    ui.kpi("Max drawdown", ui.pct(stats["max_dd"]), "over 1 year"),
    ui.kpi("52-week range", f"${lo:,.0f} – ${hi:,.0f}", f"{(last - lo) / max(hi - lo, 1e-9):.0%} of the way to the high"),
]
if "beta" in stats:
    cards.append(ui.kpi("Beta", ui.num(stats["beta"]), f"correlation {ui.num(stats['correlation'])} with S&P 500"))
ui.kpi_row(cards)

with st.container(border=True):
    window = st.segmented_control("Period", ["3M", "6M", "1Y", "Since start"], default="1Y",
                                  label_visibility="collapsed") or "1Y"
    h = ui.trim_range(px, window).dropna(subset=["spy"])
    ui.card_header(f"{ticker} vs. S&P 500", "Adjusted for splits and dividends, both indexed to 100")
    if len(h) > 1:
        ui.show(ui.indexed_growth_chart(h.date, [(ticker, h.adj_close, ui.SERIES_1, 2),
                                                 ("S&P 500 (SPY)", h.spy, ui.BENCHMARK, 1.5)]))

# ---- What-if backtest ---------------------------------------------------------------

with st.container(border=True):
    ui.card_header(f"Backtest a strategy on {ticker}",
                   "Same engine as the desk: decisions at the close, 5 bps per trade, idle cash earns the T-bill rate")
    first_day = px.date.iloc[min(60, len(px) - 1)].date()
    with st.form("what_if"):
        a, b, c, d = st.columns([2, 3, 2, 2])
        stype = a.selectbox("Strategy type", list(ui.STANDARD_PARAMS))
        params = b.text_input("Parameters", value=ui.STANDARD_PARAMS[stype],
                              help="Parameters that don't apply to the type are replaced by its standard ones")
        start = c.date_input("Start", value=max(first_day, date(2024, 1, 2)), min_value=first_day,
                             max_value=px.date.iloc[-2].date())
        capital = d.number_input("Capital ($)", min_value=1000.0, value=100_000.0, step=10_000.0)
        run = st.form_submit_button("Run backtest", type="primary")
    if run:
        ok, res = ui.api_write("POST", "/quant/backtest", {"ticker": ticker, "strategy_type": stype, "parameter": params,
                                                          "start": start.isoformat(), "capital": capital})
        st.session_state["what_if_result"] = {"ticker": ticker, "type": stype, "params": params, "start": start,
                                       "capital": capital, "result": res if ok else None, "error": None if ok else res}
    wi = st.session_state.get("what_if_result")
    if wi and wi["ticker"] == ticker:
        if wi["error"]:
            st.error(wi["error"])
        else:
            s, daily = wi["result"]["summary"], pd.DataFrame(wi["result"]["daily"])
            daily["date"] = pd.to_datetime(daily.date)
            beat = s["total_return"] >= s["buy_hold_return"]
            verdict, status = ui.verdict(s["sharpe"], s["volatility"], s["drawdown"])
            if wi["result"].get("params_note"):
                st.markdown(ui.badge("Parameter mismatch", "warning") +
                            f'<span class="qt-card-sub">&nbsp; {wi["result"]["params_note"]}</span>',
                            unsafe_allow_html=True)
            ui.kpi_row([
                ui.kpi(f"{wi['type']} return", ui.pct(s["total_return"], 1, sign=True),
                       note=f"CAGR {ui.pct(s['cagr'], 1, sign=True)}" if s.get("cagr") is not None else None),
                ui.kpi("Buy and hold", ui.pct(s["buy_hold_return"], 1, sign=True),
                       delta="Rules beat holding" if beat else "Holding did better", delta_good=beat),
                ui.kpi("Sharpe (trailing 12M)", ui.num(s["sharpe"]), note=ui.badge(verdict, status)),
                ui.kpi("Max drawdown", ui.pct(s["drawdown"]), f"volatility {ui.pct(s['volatility'])}"),
                ui.kpi("Trades", f"{s['trades']}", f"{s['trading_days']} trading days"),
            ])
            ui.show(ui.indexed_growth_chart(daily.date, [(wi["type"], daily.strategy, ui.SERIES_1, 2),
                                                         (f"Buy and hold {ticker}", daily.buy_hold, ui.BENCHMARK, 1.5)],
                                            height=300))

            # ---- Save as a live strategy ----
            with st.expander("Add this to my strategies"):
                portfolios, _ = ui.api_get(f"/portfolios/user/{st.session_state.get('user_id', 1)}")
                port_labels = {p["portfolio_id"]: f'{p["portfolio_name"]} · #{p["portfolio_id"]}'
                               for p in portfolios or []}
                with st.form("save_strategy", border=False):
                    name = st.text_input("Strategy name", value=f"{ticker} {wi['type']}")
                    port = st.selectbox("Portfolio", list(port_labels), format_func=port_labels.get)
                    if st.form_submit_button("Create strategy", type="primary"):
                        ok, asset = ui.api_write("POST", "/quant/assets", {"ticker": ticker})
                        if not ok:
                            st.error(asset)
                            st.stop()
                        qty = round(wi["capital"] / last, 4)
                        ok, trade = ui.api_write("POST", "/trades/", {"trade_type": "BUY", "quantity": qty,
                                                                      "price": round(float(last), 2),
                                                                      "trade_asset": asset["asset_id"]})
                        if not ok:
                            st.error(trade)
                            st.stop()
                        ok, strat = ui.api_write("POST", "/strategies/", {
                            "strategy_name": name, "strategy_type": wi["type"], "parameter": wi["params"],
                            "status": "inactive", "trade_strat": trade["trade_id"], "port_strat": int(port)})
                        if not ok:
                            st.error(strat)
                            st.stop()
                        ui.api_write("POST", "/quant/refresh")
                        st.success(f"Created strategy #{strat['strategy_id']} (inactive) with a seed trade of "
                                   f"{qty:,.2f} {ticker}. The backtest above is the what-if; the strategy's own "
                                   f"track record starts today and appears after the next trading days' refreshes.")

# ---- Parameter sweep ----------------------------------------------------------------

with st.container(border=True):
    ui.card_header("Test different settings",
                   "Runs the same backtest across values of the strategy's key parameter")
    sweep_types = list(ui.SWEEP_VALUES)
    a, b, c = st.columns([2, 3, 2])
    stype = a.selectbox("Strategy type", sweep_types, key="sweep_type")
    key, values = ui.SWEEP_VALUES[stype]
    chosen = b.multiselect(f"Values of {key}", values, default=values)
    start = c.date_input("Start", value=max(first_day, date(2024, 1, 2)), min_value=first_day,
                         max_value=px.date.iloc[-2].date(), key="sweep_start")
    if st.button("Run sweep", type="primary") and chosen:
        ok, res = ui.api_write("POST", "/quant/backtest/sweep", {
            "ticker": ticker, "strategy_type": stype, "parameter": ui.STANDARD_PARAMS[stype],
            "start": start.isoformat(), "values": chosen})
        st.session_state["sweep_result"] = {"ticker": ticker, "type": stype, "key": key, "result": res if ok else None,
                                     "error": None if ok else res}
    sw = st.session_state.get("sweep_result")
    if sw and sw["ticker"] == ticker:
        if sw["error"]:
            st.error(sw["error"])
        else:
            r = pd.DataFrame(sw["result"]["results"])
            best = r.loc[r.sharpe.idxmax()]
            fig = go.Figure(go.Bar(
                x=[f"{v:g}" for v in r.value], y=r.sharpe,
                marker=dict(color=[ui.SERIES_1 if v == best.value else ui.MUTED for v in r.value], cornerradius=4),
                text=[f"{v:.2f}" for v in r.sharpe], textposition="outside", textfont=dict(color=ui.INK_2),
                hovertemplate=f"{sw['key']} = %{{x}}<br>Sharpe %{{y:.2f}}<extra></extra>", cliponaxis=False,
            ))
            fig.add_hline(y=ui.SHARPE_MIN, line=dict(color=ui.INK_2, width=1, dash="dot"),
                          annotation_text="Sharpe target", annotation_font_color=ui.INK_2)
            fig.update_xaxes(title=dict(text=sw["key"], font=dict(color=ui.MUTED)), type="category")
            fig.update_yaxes(title=dict(text="Sharpe (trailing 12M)", font=dict(color=ui.MUTED)))
            left, right = st.columns([5, 7], gap="medium")
            with left:
                ui.show(ui.style_fig(fig, height=280, legend=False))
            with right:
                st.dataframe(r.assign(total_return=r.total_return * 100, buy_hold_return=r.buy_hold_return * 100,
                                      drawdown=r.drawdown * 100, volatility=r.volatility * 100),
                             hide_index=True, use_container_width=True, column_config={
                                 "value": st.column_config.NumberColumn(sw["key"], format="%g"),
                                 "total_return": st.column_config.NumberColumn("Return", format="%+.1f%%"),
                                 "buy_hold_return": st.column_config.NumberColumn("Buy & hold", format="%+.1f%%"),
                                 "sharpe": st.column_config.NumberColumn("Sharpe", format="%.2f"),
                                 "volatility": st.column_config.NumberColumn("Vol.", format="%.1f%%"),
                                 "drawdown": st.column_config.NumberColumn("Max DD", format="%.1f%%"),
                                 "trades": st.column_config.NumberColumn("Trades", format="%d"),
                             })
            st.caption(f"Best Sharpe: {sw['key']} = {best.value:g}. This is in-sample: the setting that worked "
                       "best in the past is a hypothesis to test on new data, not a forecast.")
