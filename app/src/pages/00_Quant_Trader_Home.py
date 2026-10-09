import logging
logger = logging.getLogger(__name__)

import pandas as pd
import streamlit as st
from modules.nav import SideBarLinks
from modules import quant_ui as ui

st.set_page_config(layout='wide', page_title="Trading Desk · PortIQ")

SideBarLinks()

ui.setup_page(
    f"Good morning, {st.session_state.get('first_name', 'Trader')}",
    "Your trading desk at a glance: capital, today's P&L, and every strategy checked against its risk limits.",
)

ui.refresh_panel()

scorecard, err = ui.load_scorecard()
if err:
    st.error(f"Could not load the strategy scorecard. {err}")
    st.stop()

my_ids = ui.my_portfolio_ids()
scope = st.segmented_control(
    "Scope", ["My portfolios", "Whole desk"], default="My portfolios",
    label_visibility="collapsed",
) or "My portfolios"
book = scorecard[scorecard.portfolio_id.isin(my_ids)] if scope == "My portfolios" else scorecard
if book.empty:
    st.info("No strategies in this scope yet.")
    st.stop()

# ---- KPI row ------------------------------------------------------------------

capital = book.port_value.sum()
today = book.daily_PNL.sum()
cumulative = book.cumulative_PNL.sum()
active = book[book.status.str.lower() == "active"]
breaches = active[active.verdict_status != "good"]

ui.kpi_row([
    ui.kpi("Capital deployed", ui.money(capital), f"across {book.portfolio_id.nunique()} portfolio(s)"),
    ui.kpi("Today's P&L", ui.money(today, sign=True),
           delta=ui.pct(today / (capital - today), 2, sign=True), delta_good=today >= 0, note="vs. yesterday"),
    ui.kpi("Cumulative P&L", ui.money(cumulative, sign=True),
           delta=ui.pct(cumulative / (capital - cumulative), 1, sign=True), delta_good=cumulative >= 0,
           note="since inception"),
    ui.kpi("Active strategies", f"{len(active)} / {len(book)}", "running now"),
    ui.kpi("Risk-limit alerts", str(len(breaches)),
           delta=f"{len(breaches) / max(len(active), 1):.0%} of active", delta_good=len(breaches) == 0,
           note="need review"),
])

# ---- Alerts + capital curve ---------------------------------------------------

left, right = st.columns([5, 7], gap="medium")

with left:
    with st.container(border=True):
        ui.card_header("Needs your attention",
                       "Active strategies outside a risk limit, worst drawdown first")
        if breaches.empty:
            st.markdown(ui.badge("All active strategies are within limits", "good"), unsafe_allow_html=True)
        else:
            worst = breaches.sort_values("drawdown", ascending=False)
            rows_html = []
            for r in worst.head(5).itertuples():
                if r.drawdown > ui.DRAWDOWN_MAX:
                    rule = f"Drawdown <b>{ui.pct(r.drawdown)}</b> vs {ui.pct(ui.DRAWDOWN_MAX, 0)} limit"
                elif r.sharpe_ratio < ui.SHARPE_MIN:
                    rule = f"Sharpe <b>{ui.num(r.sharpe_ratio)}</b> vs {ui.SHARPE_MIN:.1f} target"
                else:
                    rule = f"Volatility <b>{ui.pct(r.volatility)}</b> vs {ui.pct(ui.VOL_MAX, 0)} limit"
                rows_html.append(
                    f'<div class="qt-alert"><div><div class="qt-alert-name">{r.strategy_name}</div>'
                    f'<div class="qt-alert-meta">{r.strategy_type} · {ui.money(r.port_value)} capital</div></div>'
                    f'<div class="qt-alert-rule">{ui.badge(r.verdict, r.verdict_status)}<br>{rule}</div></div>'
                )
            st.markdown("".join(rows_html), unsafe_allow_html=True)
            if len(worst) > 5:
                st.caption(f"+ {len(worst) - 5} more in Risk Analysis")
            pick = st.selectbox(
                "Review a strategy", worst.strategy_id,
                format_func=lambda i: worst.set_index("strategy_id").strategy_name[i],
                label_visibility="collapsed",
            )
            if st.button("Open in Risk Analysis →", use_container_width=True, type="primary"):
                st.session_state["selected_strategy"] = int(pick)
                st.switch_page("pages/03_Risk_Analysis.py")

with right:
    with st.container(border=True):
        ui.card_header("Strategy returns vs. S&P 500", "Last 6 months, time-weighted, both indexed to 100")
        ids = ",".join(str(i) for i in my_ids) if scope == "My portfolios" else ""
        hist, herr = ui.api_get("/quant/history", portfolio_ids=ids) if ids or scope == "Whole desk" else ([], None)
        if herr or not hist:
            st.info("No history available.")
        else:
            h = ui.trim_range(ui.to_frame(hist, ("growth_index", "spy_close")).dropna(), "6M")
            h["record_date"] = pd.to_datetime(h.record_date)
            fig = ui.indexed_growth_chart(h.record_date, [
                ("Strategies", h.growth_index, ui.SERIES_1, 2),
                ("S&P 500 (SPY)", h.spy_close, ui.BENCHMARK, 1.5),
            ], height=330)
            ui.show(fig)

# ---- Scorecard ----------------------------------------------------------------

with st.container(border=True):
    ui.card_header("Strategy scorecard",
                   f"{len(book)} strategies · verdicts use Sharpe ≥ {ui.SHARPE_MIN:.1f}, "
                   f"volatility ≤ {ui.pct(ui.VOL_MAX, 0)}, drawdown ≤ {ui.pct(ui.DRAWDOWN_MAX, 0)}")
    severity = {"critical": 0, "warning": 1, "good": 2}
    table = book.assign(_sev=book.verdict_status.map(severity)).sort_values(
        ["_sev", "sharpe_ratio"], ascending=[True, False]).assign(
        vol_pct=lambda d: d.volatility * 100,
        dd_pct=lambda d: d.drawdown * 100,
        status=lambda d: d.status.str.capitalize(),
    )
    st.dataframe(
        table[["strategy_name", "strategy_type", "status", "verdict_label", "port_value", "daily_PNL",
               "cumulative_PNL", "sharpe_ratio", "vol_pct", "dd_pct", "sparkline"]],
        hide_index=True, use_container_width=True, height=min(38 + 35 * len(table), 420),
        column_config={
            "strategy_name": st.column_config.TextColumn("Strategy", width=122),
            "strategy_type": st.column_config.TextColumn("Type", width=108),
            "status": st.column_config.TextColumn("Status", width=60),
            "verdict_label": st.column_config.TextColumn("Risk verdict", width=150),
            "port_value": st.column_config.NumberColumn("Capital ($)", format=ui.MONEY, width=85),
            "daily_PNL": st.column_config.NumberColumn("Today ($)", format=ui.MONEY, width=75),
            "cumulative_PNL": st.column_config.NumberColumn("Total P&L ($)", format=ui.MONEY, width=95),
            "sharpe_ratio": st.column_config.ProgressColumn("Sharpe", min_value=0, max_value=2.5,
                                                            format="%.2f", width=90),
            "vol_pct": st.column_config.NumberColumn("Vol.", format="%.1f%%", width=55),
            "dd_pct": st.column_config.NumberColumn("Drawdown", format="%.1f%%", width=75),
            "sparkline": st.column_config.LineChartColumn("30-day trend", width=90),
        },
    )

# ---- Shortcuts ----------------------------------------------------------------

c1, c2, c3, c4 = st.columns(4)
if c1.button("Portfolio performance", use_container_width=True):
    st.switch_page("pages/01_Portfolio_Performance.py")
if c2.button("Strategy vs. benchmark", use_container_width=True):
    st.switch_page("pages/02_Strategy_Benchmark.py")
if c3.button("Risk analysis", use_container_width=True):
    st.switch_page("pages/03_Risk_Analysis.py")
if c4.button("Trading logs", use_container_width=True):
    st.switch_page("pages/04_Trading_Logs.py")
