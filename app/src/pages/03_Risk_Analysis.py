import logging
logger = logging.getLogger(__name__)

import plotly.graph_objects as go
import streamlit as st
from modules.nav import SideBarLinks
from modules import quant_ui as ui

st.set_page_config(layout='wide', page_title="Risk Analysis · PortIQ")

SideBarLinks()

ui.setup_page("Risk Analysis",
              "Every strategy checked against the desk's three risk rules, using trailing 12-month metrics "
              "from backtests on real prices.")

scorecard, err = ui.load_scorecard()
if err:
    st.error(f"Could not load risk metrics. {err}")
    st.stop()
scorecard = scorecard.dropna(subset=["sharpe_ratio"])
my_ids = ui.my_portfolio_ids()

# ---- Filters ------------------------------------------------------------------

f1, f2, f3 = st.columns([2, 5, 1])
scope = f1.segmented_control("Scope", ["My portfolios", "Whole desk"], default="Whole desk") or "Whole desk"
types = sorted(scorecard.strategy_type.unique())
chosen = f2.pills("Strategy types", types, default=types, selection_mode="multi") or types
active_only = f3.toggle("Active only", value=False)

view = scorecard[scorecard.portfolio_id.isin(my_ids)] if scope == "My portfolios" else scorecard
view = view[view.strategy_type.isin(chosen)]
if active_only:
    view = view[view.status.str.lower() == "active"]
if view.empty:
    st.info("No strategies match these filters.")
    st.stop()

# ---- KPIs ---------------------------------------------------------------------

healthy = view[view.verdict_status == "good"]
dd_breach = view[view.drawdown > ui.DRAWDOWN_MAX]
active_breach = dd_breach[dd_breach.status.str.lower() == "active"]
under_review = view[view.verdict_status != "good"]
ui.kpi_row([
    ui.kpi("Strategies", str(len(view)), f"{view.strategy_type.nunique()} types"),
    ui.kpi("Pass all 3 rules", str(len(healthy)),
           delta=f"{len(healthy) / len(view):.0%}", delta_good=len(healthy) / len(view) >= 0.5, note="of strategies"),
    ui.kpi("Meet Sharpe target", f"{(view.sharpe_ratio >= ui.SHARPE_MIN).mean():.0%}",
           note=f"Sharpe ≥ {ui.SHARPE_MIN:.1f}"),
    ui.kpi("Drawdown breaches", str(len(dd_breach)),
           delta=f"{len(active_breach)} still active", delta_good=len(active_breach) == 0),
    ui.kpi("Capital under review", ui.money(under_review.port_value.sum()),
           note=f"{under_review.port_value.sum() / view.port_value.sum():.0%} of capital in view"),
])

# ---- Risk map + failing rules -------------------------------------------------

left, right = st.columns([8, 4], gap="medium")

with left:
    with st.container(border=True):
        ui.card_header("Risk map: risk-adjusted return vs. drawdown",
                       "Top-left is healthy. Bubble size is capital. Dotted lines are the limits.")
        fig = go.Figure()
        max_cap = view.port_value.max()
        for status, name in [("good", "Healthy"), ("warning", "Watch"), ("critical", "Review")]:
            d = view[view.verdict_status == status]
            if d.empty:
                continue
            fig.add_trace(go.Scatter(
                x=d.drawdown * 100, y=d.sharpe_ratio, mode="markers",
                name=f"{ui.STATUS_ICON[status]} {name} ({len(d)})",
                marker=dict(color=ui.STATUS_COLOR[status], opacity=0.85,
                            size=10 + 26 * (d.port_value / max_cap) ** 0.5,
                            line=dict(color=ui.SURFACE, width=2)),
                customdata=d[["strategy_name", "strategy_type", "volatility", "port_value", "verdict"]],
                hovertemplate=("<b>%{customdata[0]}</b> · %{customdata[1]}<br>"
                               "Sharpe %{y:.2f} · drawdown %{x:.1f}% · vol %{customdata[2]:.0%}<br>"
                               "$%{customdata[3]:,.0f} capital · %{customdata[4]}<extra></extra>"),
            ))
        fig.add_vline(x=ui.DRAWDOWN_MAX * 100, line=dict(color=ui.INK_2, width=1, dash="dot"))
        fig.add_hline(y=ui.SHARPE_MIN, line=dict(color=ui.INK_2, width=1, dash="dot"))
        fig.add_annotation(xref="paper", yref="paper", x=0.01, y=1.0, text="↖ Healthy zone", showarrow=False,
                           xanchor="left", yanchor="top", font=dict(color=ui.MUTED, size=11))
        fig.add_annotation(xref="paper", yref="paper", x=0.99, y=0.0, text="Worst zone ↘", showarrow=False,
                           xanchor="right", yanchor="bottom", font=dict(color=ui.MUTED, size=11))
        fig.update_xaxes(title=dict(text="Maximum drawdown", font=dict(color=ui.MUTED)), ticksuffix="%",
                         showgrid=True, gridcolor=ui.GRID, rangemode="tozero")
        fig.update_yaxes(title=dict(text="Sharpe ratio", font=dict(color=ui.MUTED)))
        ui.show(ui.style_fig(fig, height=430))

with right:
    with st.container(border=True):
        ui.card_header("Which rules fail", "Strategies outside each limit")
        rules = [
            (f"Drawdown > {ui.pct(ui.DRAWDOWN_MAX, 0)}", int((view.drawdown > ui.DRAWDOWN_MAX).sum())),
            (f"Volatility > {ui.pct(ui.VOL_MAX, 0)}", int((view.volatility > ui.VOL_MAX).sum())),
            (f"Sharpe < {ui.SHARPE_MIN:.1f}", int((view.sharpe_ratio < ui.SHARPE_MIN).sum())),
        ]
        fig = go.Figure(go.Bar(
            x=[c for _, c in rules][::-1], y=[r for r, _ in rules][::-1], orientation="h",
            marker=dict(color=ui.SERIES_1, cornerradius=4),
            text=[f"{c} ({c / len(view):.0%})" for _, c in rules][::-1], textposition="outside",
            textfont=dict(color=ui.INK_2), cliponaxis=False,
            hovertemplate="%{y}: %{x} strategies<extra></extra>",
        ))
        fig.update_xaxes(showticklabels=False, range=[0, max(c for _, c in rules) * 1.45 + 1])
        fig = ui.style_fig(fig, height=190, legend=False)
        fig.update_layout(bargap=0.45)
        ui.show(fig)

    with st.container(border=True):
        ui.card_header("By strategy type")
        by_type = (view.assign(breach=view.verdict_status != "good")
                   .groupby("strategy_type")
                   .agg(n=("strategy_id", "count"), sharpe=("sharpe_ratio", "mean"), breach=("breach", "mean"))
                   .sort_values("sharpe", ascending=False).reset_index())
        st.dataframe(by_type.assign(breach=by_type.breach * 100), hide_index=True, use_container_width=True,
                     column_config={
                         "strategy_type": st.column_config.TextColumn("Type", width=110),
                         "n": st.column_config.NumberColumn("#", width=35),
                         "sharpe": st.column_config.NumberColumn("Sharpe", format="%.2f", width=55),
                         "breach": st.column_config.NumberColumn("Under review", format="%.0f%%"),
                     })

# ---- Drill-down ---------------------------------------------------------------

with st.container(border=True):
    ui.card_header("Strategy drill-down", "Each metric against its limit")
    ordered = view.sort_values("drawdown", ascending=False)
    ids = list(ordered.strategy_id)
    pre = st.session_state.get("selected_strategy")
    labels = ordered.set_index("strategy_id")
    c1, c2 = st.columns([4, 2])
    strategy_id = c1.selectbox(
        "Strategy", ids, index=ids.index(pre) if pre in ids else 0, label_visibility="collapsed",
        format_func=lambda i: f"{ui.VERDICT_ICON[labels.verdict_status[i]]} {labels.strategy_name[i]} · "
                              f"{labels.strategy_type[i]} · #{i}",
    )
    st.session_state["selected_strategy"] = strategy_id
    if c2.button("Compare with benchmark →", use_container_width=True):
        st.switch_page("pages/02_Strategy_Benchmark.py")
    r = labels.loc[strategy_id]
    cols = st.columns(3, gap="medium")
    cols[0].markdown(ui.rule_card(
        "Sharpe ratio", ui.num(r.sharpe_ratio), f"Target ≥ {ui.SHARPE_MIN:.1f}", r.sharpe_ratio >= ui.SHARPE_MIN,
        "Return earned per unit of risk. Below 1.0, the return may not justify the risk taken.",
        r.sharpe_ratio / 2.5, ui.SHARPE_MIN / 2.5), unsafe_allow_html=True)
    cols[1].markdown(ui.rule_card(
        "Volatility", ui.pct(r.volatility), f"Limit ≤ {ui.pct(ui.VOL_MAX, 0)}", r.volatility <= ui.VOL_MAX,
        "How much returns swing, annualized. Higher means a bumpier and less predictable ride.",
        r.volatility / 0.35, ui.VOL_MAX / 0.35), unsafe_allow_html=True)
    cols[2].markdown(ui.rule_card(
        "Maximum drawdown", ui.pct(r.drawdown), f"Limit ≤ {ui.pct(ui.DRAWDOWN_MAX, 0)}",
        r.drawdown <= ui.DRAWDOWN_MAX,
        "The largest fall from a peak. Past 10%, the strategy should be reviewed or resized.",
        r.drawdown / 0.25, ui.DRAWDOWN_MAX / 0.25), unsafe_allow_html=True)

# ---- Full table ---------------------------------------------------------------

with st.container(border=True):
    ui.card_header("All strategies in view")
    table = view.sort_values("drawdown", ascending=False).assign(
        status=lambda d: d.status.str.capitalize(), vol_pct=lambda d: d.volatility * 100,
        dd_pct=lambda d: d.drawdown * 100)
    cols = ["strategy_name", "strategy_type", "portfolio_name", "status", "verdict_label",
            "sharpe_ratio", "vol_pct", "dd_pct", "port_value"]
    st.dataframe(table[cols], hide_index=True, use_container_width=True, height=360, column_config={
        "strategy_name": "Strategy", "strategy_type": "Type", "portfolio_name": "Portfolio", "status": "Status",
        "verdict_label": "Risk verdict",
        "sharpe_ratio": st.column_config.NumberColumn("Sharpe", format="%.2f"),
        "vol_pct": st.column_config.NumberColumn("Volatility", format="%.1f%%"),
        "dd_pct": st.column_config.NumberColumn("Drawdown", format="%.1f%%"),
        "port_value": st.column_config.NumberColumn("Capital ($)", format=ui.MONEY),
    })
    st.download_button("Download CSV", table[cols].to_csv(index=False), "portiq_risk_metrics.csv",
                       mime="text/csv")
