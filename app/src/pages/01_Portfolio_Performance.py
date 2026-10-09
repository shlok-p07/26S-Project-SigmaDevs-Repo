import logging
logger = logging.getLogger(__name__)

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from modules.nav import SideBarLinks
from modules import quant_ui as ui

st.set_page_config(layout='wide', page_title="Portfolio Performance · PortIQ")

SideBarLinks()

ui.setup_page("Portfolio Performance",
              "Growth against the market, what the portfolio holds, and how concentrated it is.")

portfolios, err = ui.api_get("/portfolios/")
if err:
    st.error(f"Could not load portfolios. {err}")
    st.stop()
portfolios = ui.to_frame(portfolios, ("total_value",))
my_ids = ui.my_portfolio_ids()

# ---- Filters ------------------------------------------------------------------

f1, f2, f3 = st.columns([4, 2, 3])
show_all = f2.toggle("Include desk portfolios", value=False)
options = portfolios if show_all else portfolios[portfolios.portfolio_id.isin(my_ids)]
labels = {r.portfolio_id: f"{r.portfolio_name} · #{r.portfolio_id}" for r in options.itertuples()}
portfolio_id = f1.selectbox("Portfolio", list(labels), format_func=labels.get)
window = f3.segmented_control("Period", list(ui.RANGE_DAYS), default="1Y") or "1Y"

pf = portfolios.set_index("portfolio_id").loc[portfolio_id]
positions, perr = ui.api_get(f"/quant/portfolios/{portfolio_id}/positions")
positions = ui.to_frame(positions or [], ("market_value", "unrealized_PNL", "avg_cost", "qty_held",
                                          "price_target", "avg_rating"))
hist, herr = ui.api_get("/quant/history", portfolio_ids=str(portfolio_id))
hist = ui.to_frame(hist or [], ("strategy_capital", "daily_PNL", "spy_close", "growth_index"))
hist = hist.dropna(subset=["spy_close"]) if not hist.empty else hist
period = "since start" if window == "Since start" else window

# ---- KPIs ---------------------------------------------------------------------

cards = []
if not positions.empty:
    cards.append(ui.kpi("Holdings value", ui.money(positions.market_value.sum()),
                        f"at the latest close · confidence {pf.confidence}"))
if not hist.empty:
    h = ui.trim_range(hist, window)
    stats = ui.perf_stats(h.growth_index, h.spy_close)
    today = hist.daily_PNL.iloc[-1]
    cards += [
        ui.kpi("Today's P&L", ui.money(today, sign=True),
               delta=ui.pct(today / (hist.strategy_capital.iloc[-1] - today), 2, sign=True),
               delta_good=today >= 0, note="strategy capital"),
        ui.kpi(f"Return {period}", ui.pct(stats["return"], 1, sign=True),
               delta=f"{ui.pct(stats['excess'], 1, sign=True)} vs S&P 500", delta_good=stats["excess"] >= 0),
        ui.kpi(f"Max drawdown {period}", ui.pct(stats["max_dd"]),
               note=f"volatility {ui.pct(stats['vol'])} annualized"),
    ]
hhi = None
if not positions.empty:
    weights = positions.market_value / positions.market_value.sum()
    hhi = float((weights.mul(100) ** 2).sum())
    status = "critical" if hhi > 2500 else ("warning" if hhi > 1500 else "good")
    label = {"critical": "Highly concentrated", "warning": "Moderately concentrated",
             "good": "Diversified"}[status]
    cards += [
        ui.kpi("Unrealized P&L", ui.money(positions.unrealized_PNL.sum(), sign=True),
               note=f"on {ui.money(positions.market_value.sum())} of holdings"),
        ui.kpi("Concentration (HHI)", f"{hhi:,.0f}", note=ui.badge(label, status)),
    ]
ui.kpi_row(cards)

# ---- Growth chart -------------------------------------------------------------

with st.container(border=True):
    ui.card_header("Strategy returns vs. S&P 500",
                   f"{'Since the first strategy started' if window == 'Since start' else 'Last ' + window}, "
                   "time-weighted, both indexed to 100")
    if hist.empty:
        st.info("This portfolio has no strategies with history yet.")
    else:
        h = ui.trim_range(hist, window)
        h["record_date"] = pd.to_datetime(h.record_date)
        ui.show(ui.indexed_growth_chart(h.record_date, [
            ("Strategies", h.growth_index, ui.SERIES_1, 2),
            ("S&P 500 (SPY)", h.spy_close, ui.BENCHMARK, 1.5),
        ]))

# ---- Holdings -----------------------------------------------------------------

left, right = st.columns([4, 8], gap="medium")

with left:
    with st.container(border=True):
        ui.card_header("Allocation", "Share of holdings by market value")
        if positions.empty:
            st.info("No positions recorded.")
        else:
            # Several positions can hold the same ticker; chart them as one bar
            alloc = (positions.assign(weight=weights)
                     .groupby("ticker", as_index=False)
                     .agg(weight=("weight", "sum"), market_value=("market_value", "sum"),
                          asset_name=("asset_name", "first"))
                     .sort_values("weight"))
            fig = go.Figure(go.Bar(
                x=alloc.weight * 100, y=alloc.ticker, orientation="h",
                marker=dict(color=ui.SERIES_1, cornerradius=4),
                text=[f"{w:.1%}" for w in alloc.weight], textposition="outside",
                textfont=dict(color=ui.INK_2),
                customdata=alloc[["asset_name", "market_value"]],
                hovertemplate="<b>%{y}</b> · %{customdata[0]}<br>%{x:.1f}% · $%{customdata[1]:,.0f}<extra></extra>",
                cliponaxis=False,
            ))
            fig.update_xaxes(ticksuffix="%", showgrid=True, gridcolor=ui.GRID, range=[0, alloc.weight.max() * 118])
            fig = ui.style_fig(fig, height=max(200, 60 + 40 * len(alloc)), legend=False)
            fig.update_layout(bargap=0.5)
            ui.show(fig)
            top = alloc.iloc[-1]
            if hhi and hhi > 2500:
                st.markdown(
                    ui.badge(f"{top.ticker} is {top.weight:.0%} of holdings: consider rebalancing", "warning"),
                    unsafe_allow_html=True,
                )

with right:
    with st.container(border=True):
        ui.card_header("Holdings", "Cost basis is the real close on the purchase date. "
                                   "Price targets and analyst ratings are sample data.")
        if not positions.empty:
            table = positions.sort_values("market_value", ascending=False).assign(
                weight_pct=weights * 100,
                upside_pct=(positions.price_target / (positions.market_value / positions.qty_held) - 1) * 100,
                return_pct=positions.unrealized_PNL / (positions.avg_cost * positions.qty_held) * 100,
            )
            st.dataframe(
                table[["ticker", "asset_name", "market_value", "weight_pct",
                       "unrealized_PNL", "return_pct", "upside_pct", "avg_rating"]],
                hide_index=True, use_container_width=True,
                column_config={
                    "ticker": st.column_config.TextColumn("Ticker", width=60),
                    "asset_name": st.column_config.TextColumn("Name", width=150),
                    "market_value": st.column_config.NumberColumn("Value ($)", format=ui.MONEY, width=80),
                    "weight_pct": st.column_config.ProgressColumn("Weight", min_value=0, max_value=100,
                                                                  format="%.1f%%", width=100),
                    "unrealized_PNL": st.column_config.NumberColumn("Unrealized ($)", format=ui.MONEY,
                                                                    width=95),
                    "return_pct": st.column_config.NumberColumn("Return", format="%.1f%%", width=65),
                    "upside_pct": st.column_config.NumberColumn("To target", format="%.1f%%", width=70),
                    "avg_rating": st.column_config.NumberColumn("Rating", format="%.1f ★", width=60),
                },
            )

# ---- Strategies in this portfolio --------------------------------------------

scorecard, _ = ui.load_scorecard()
if scorecard is not None:
    mine = scorecard[scorecard.portfolio_id == portfolio_id]
    if not mine.empty:
        with st.container(border=True):
            ui.card_header("Strategies running in this portfolio")
            st.dataframe(
                mine.assign(status=mine.status.str.capitalize())[
                    ["strategy_name", "strategy_type", "status", "verdict_label", "port_value",
                     "cumulative_PNL", "sparkline"]],
                hide_index=True, use_container_width=True,
                column_config={
                    "strategy_name": "Strategy", "strategy_type": "Type", "status": "Status",
                    "verdict_label": "Risk verdict",
                    "port_value": st.column_config.NumberColumn("Capital ($)", format=ui.MONEY),
                    "cumulative_PNL": st.column_config.NumberColumn("Total P&L ($)", format=ui.MONEY),
                    "sparkline": st.column_config.LineChartColumn("30-day trend"),
                },
            )
