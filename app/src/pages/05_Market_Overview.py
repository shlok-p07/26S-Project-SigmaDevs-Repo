import logging
logger = logging.getLogger(__name__)

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from modules.nav import SideBarLinks
from modules import quant_ui as ui

st.set_page_config(layout='wide', page_title="Market Overview · PortIQ")

SideBarLinks()

ui.setup_page("Market Overview",
              "Breadth, leaders, and laggards across every U.S.-listed stock, from the latest daily close.")

universe = ui.universe_status()
st.caption(ui.universe_line(universe))
job = universe.get("latest_job") or {}
if job.get("status") == "running" and (job.get("message") or "").split(" of ")[0].isdigit():
    done, _, rest = job["message"].partition(" of ")
    total = int(rest.split()[0])
    st.progress(min(int(done) / max(total, 1), 1.0), text=f"Loading prices: {int(done):,} of {total:,} securities")

data, err = ui.api_get("/quant/market/overview")
if err:
    st.info("The whole-market data hasn't loaded yet. It loads automatically after the close each weekday; "
            "the first load takes about 30 minutes. You can also run `docker exec web-api python -m "
            "backend.market.universe`.")
    st.stop()

b = data["breadth"]
as_of = pd.Timestamp(data["as_of"]).strftime("%b %d, %Y")
adv, dec = int(b["advancers"] or 0), int(b["decliners"] or 0)
above_50 = (b["above_50"] or 0) / max(b["with_50"] or 1, 1)
above_200 = (b["above_200"] or 0) / max(b["with_200"] or 1, 1)
highs, lows = int(b["new_highs"] or 0), int(b["new_lows"] or 0)

ui.kpi_row([
    ui.kpi("Stocks tracked", f"{int(b['stocks']):,}", "trading at least $1M a day"),
    ui.kpi("Advancers vs. decliners", f"{adv:,} / {dec:,}",
           delta=f"{adv / max(adv + dec, 1):.0%} up", delta_good=adv >= dec, note=f"on {as_of}"),
    ui.kpi("Above 50-day average", f"{above_50:.0%}", "short-term trend"),
    ui.kpi("Above 200-day average", f"{above_200:.0%}", "long-term trend"),
    ui.kpi("52-week highs vs. lows", f"{highs:,} / {lows:,}",
           delta="more highs" if highs >= lows else "more lows", delta_good=highs >= lows),
    ui.kpi("Dollar volume", ui.money(float(b["dollar_volume"] or 0)), "traded across these stocks"),
])

# ---- Indexes --------------------------------------------------------------------

names = {"SPY": "S&P 500", "QQQ": "Nasdaq-100", "IWM": "Russell 2000", "DIA": "Dow Jones"}
indexes = {i["ticker"]: i for i in data["indexes"]}
cards = []
for t in ("SPY", "QQQ", "IWM", "DIA"):
    i = indexes.get(t)
    if i:
        cards.append(ui.kpi(f"{names[t]} ({t})", f"${i['close_value']:,.2f}",
                            delta=ui.pct(i["change_pct"], 2, sign=True), delta_good=(i["change_pct"] or 0) >= 0,
                            note=f"1Y {ui.pct(i['ret_1y'], 1, sign=True)} · 3M {ui.pct(i['ret_3m'], 1, sign=True)}"))
if cards:
    ui.kpi_row(cards)

# ---- Return distribution + exchanges ----------------------------------------------

left, right = st.columns([7, 5], gap="medium")
with left:
    with st.container(border=True):
        returns = pd.Series(data["returns_1y"]) * 100
        spy_1y = (indexes.get("SPY") or {}).get("ret_1y")
        ui.card_header("How U.S. stocks did over the last year",
                       f"1-year returns of {len(returns):,} stocks; the gray line is the S&P 500")
        clipped = returns.clip(-100, 200)
        fig = go.Figure(go.Histogram(
            x=clipped, xbins=dict(start=-100, end=200, size=10),
            marker=dict(color=ui.SERIES_1, line=dict(color=ui.SURFACE, width=2)),
            hovertemplate="%{x}% return: %{y} stocks<extra></extra>",
        ))
        if spy_1y is not None:
            fig.add_vline(x=spy_1y * 100, line=dict(color=ui.INK_2, width=1.5),
                          annotation_text=f"S&P 500 {spy_1y * 100:+.0f}%", annotation_font_color=ui.INK_2)
        fig.add_vline(x=0, line=dict(color=ui.AXIS, width=1))
        fig.update_xaxes(ticksuffix="%", title=None)
        fig.update_yaxes(title=None)
        ui.show(ui.style_fig(fig, height=300, legend=False))
        beat = f"{(returns > spy_1y * 100).mean():.0%} of stocks beat the S&P 500 · " if spy_1y is not None else ""
        st.caption(f"Median stock {returns.median():+.1f}% · {beat}"
                   f"{(returns < 0).mean():.0%} fell · returns above 200% shown at 200%")

with right:
    with st.container(border=True):
        ui.card_header("By exchange", "Stocks trading at least $1M a day")
        ex = pd.DataFrame(data["by_exchange"])
        if not ex.empty:
            ex = ex.assign(avg_ret_1y=ex.avg_ret_1y * 100, pct_up=ex.pct_up * 100)
            st.dataframe(ex[["exchange", "n", "pct_up", "avg_ret_1y"]],
                         hide_index=True, use_container_width=True, column_config={
                             "exchange": "Exchange", "n": st.column_config.NumberColumn("Stocks", format="%,d"),
                             "avg_ret_1y": st.column_config.NumberColumn("Avg 1Y return", format="%+.1f%%"),
                             "pct_up": st.column_config.NumberColumn("Up today", format="%.0f%%"),
                         })

# ---- Movers --------------------------------------------------------------------

with st.container(border=True):
    ui.card_header("Movers", f"{as_of} · gainers and losers are limited to stocks trading at least $20M a day")
    tabs = st.tabs(["Top gainers", "Top losers", "Most active"])
    picked = None
    for tab, key in zip(tabs, ("gainers", "losers", "most_active")):
        with tab:
            m = pd.DataFrame(data["movers"][key])
            if m.empty:
                st.info("No data.")
                continue
            m = m.assign(change_pct=m.change_pct * 100, ret_1m=m.ret_1m * 100, ret_1y=m.ret_1y * 100)
            st.dataframe(m[["ticker", "name", "exchange", "close_value", "change_pct", "dollar_volume",
                            "ret_1m", "ret_1y"]], hide_index=True, use_container_width=True, column_config={
                "ticker": st.column_config.TextColumn("Ticker", width=70), "name": "Name", "exchange": "Exchange",
                "close_value": st.column_config.NumberColumn("Close ($)", format="%,.2f"),
                "change_pct": st.column_config.NumberColumn("Today", format="%+.1f%%"),
                "dollar_volume": st.column_config.NumberColumn("Traded ($)", format="compact"),
                "ret_1m": st.column_config.NumberColumn("1M", format="%+.1f%%"),
                "ret_1y": st.column_config.NumberColumn("1Y", format="%+.1f%%"),
            })
    c1, c2 = st.columns([3, 1])
    choices = sorted({r["ticker"] for k in data["movers"] for r in data["movers"][k]})
    pick = c1.selectbox("Look up a stock", choices, label_visibility="collapsed")
    if c2.button("Open in Stock Lookup →", use_container_width=True):
        st.session_state["lookup_ticker"] = pick
        st.switch_page("pages/06_Stock_Lookup.py")
