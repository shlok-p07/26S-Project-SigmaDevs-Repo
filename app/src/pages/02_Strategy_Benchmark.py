import logging
logger = logging.getLogger(__name__)

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from modules.nav import SideBarLinks
from modules import quant_ui as ui

st.set_page_config(layout='wide', page_title="Strategy vs Benchmark · PortIQ")

SideBarLinks()

ui.setup_page("Strategy vs Benchmark",
              "Is the strategy beating its benchmark, how much market risk it takes, and where the gap came from.")

scorecard, err = ui.load_scorecard()
if err:
    st.error(f"Could not load strategies. {err}")
    st.stop()
my_ids = ui.my_portfolio_ids()

# ---- Filters ------------------------------------------------------------------

f1, f2, f3 = st.columns([4, 2, 3])
desk = f2.toggle("Include desk strategies", value=False)
pool = scorecard if desk else scorecard[scorecard.portfolio_id.isin(my_ids)]
labels = {r.strategy_id: f"{ui.VERDICT_ICON[r.verdict_status]} {r.strategy_name} · {r.strategy_type} · #{r.strategy_id}"
          for r in pool.itertuples()}
preselected = st.session_state.get("selected_strategy")
if preselected not in labels and preselected in scorecard.strategy_id.values:
    pool, desk = scorecard, True
    labels = {r.strategy_id: f"{ui.VERDICT_ICON[r.verdict_status]} {r.strategy_name} · {r.strategy_type} · #{r.strategy_id}"
              for r in pool.itertuples()}
ids = list(labels)
strategy_id = f1.selectbox("Strategy", ids, format_func=labels.get,
                           index=ids.index(preselected) if preselected in ids else 0)
st.session_state["selected_strategy"] = strategy_id
window = f3.segmented_control("Period", list(ui.RANGE_DAYS), default="1Y") or "1Y"

s = scorecard.set_index("strategy_id").loc[strategy_id]

st.markdown(
    f'{ui.badge(s.status.capitalize(), "good" if s.status.lower() == "active" else "warning")} '
    f'{ui.badge(s.verdict, s.verdict_status)} '
    f'<span class="qt-card-sub">&nbsp; Parameters <code>{s.parameter}</code> · '
    f'Benchmark {s.benchmark_name} ({s.benchmark_ticker}) · {s.portfolio_name}</span>',
    unsafe_allow_html=True,
)

hist, herr = ui.api_get(f"/quant/strategies/{strategy_id}/history")
if herr:
    st.info("No daily history for this strategy yet.")
    st.stop()
full = ui.to_frame(hist, ("port_value", "daily_PNL", "benchmark_close", "growth_index")).dropna(
    subset=["benchmark_close"])
full["record_date"] = pd.to_datetime(full.record_date)
h = ui.trim_range(full, window)
stats = ui.perf_stats(h.growth_index, h.benchmark_close)
period = "since start" if window == "Since start" else window

# ---- KPIs ---------------------------------------------------------------------

ui.kpi_row([
    ui.kpi(f"Strategy {period}", ui.pct(stats["return"], 1, sign=True),
           note=f"{ui.money(h.daily_PNL.iloc[1:].sum(), sign=True)} P&L"),
    ui.kpi(f"{s.benchmark_ticker} {period}", ui.pct(stats["bench_return"], 1, sign=True), note="benchmark"),
    ui.kpi("Excess return", ui.pct(stats["excess"], 1, sign=True),
           delta="Beating" if stats["excess"] >= 0 else "Lagging", delta_good=stats["excess"] >= 0,
           note="the benchmark"),
    ui.kpi("Beta", ui.num(stats["beta"]), note=f"correlation {ui.num(stats['correlation'])}"),
    ui.kpi("Information ratio", ui.num(stats["info_ratio"]), note="excess return vs. tracking risk"),
    ui.kpi(f"Max drawdown {period}", ui.pct(stats["max_dd"]),
           delta="Within limit" if stats["max_dd"] <= ui.DRAWDOWN_MAX else "Over limit",
           delta_good=stats["max_dd"] <= ui.DRAWDOWN_MAX, note=f"limit {ui.pct(ui.DRAWDOWN_MAX, 0)}"),
])

# ---- Growth -------------------------------------------------------------------

with st.container(border=True):
    ui.card_header(f"{s.strategy_name} vs. {s.benchmark_name}",
                   f"{'Since the strategy started' if window == 'Since start' else 'Last ' + window}, "
                   "time-weighted, both indexed to 100")
    ui.show(ui.indexed_growth_chart(h.record_date, [
        (s.strategy_name, h.growth_index, ui.SERIES_1, 2),
        (f"{s.benchmark_ticker} benchmark", h.benchmark_close, ui.BENCHMARK, 1.5),
    ], height=360))

# ---- Relative performance + drawdown -----------------------------------------

left, right = st.columns(2, gap="medium")
strat_idx = h.growth_index / h.growth_index.iloc[0]
bench_idx = h.benchmark_close / h.benchmark_close.iloc[0]

with left:
    with st.container(border=True):
        ui.card_header("Cumulative excess return", "Strategy growth minus benchmark growth; above zero means ahead")
        excess = (strat_idx - bench_idx) * 100
        fig = go.Figure(go.Scatter(
            x=h.record_date, y=excess, mode="lines", line=dict(color=ui.SERIES_1, width=2),
            fill="tozeroy", fillcolor="rgba(57,135,229,0.12)",
            hovertemplate="%{x|%b %d}: %{y:+.1f} pts<extra></extra>",
        ))
        fig.add_hline(y=0, line=dict(color=ui.AXIS, width=1))
        fig.update_yaxes(ticksuffix=" pts")
        ui.show(ui.style_fig(fig, height=260, legend=False))

with right:
    with st.container(border=True):
        ui.card_header("Drawdown from peak", f"Red line marks the {ui.pct(ui.DRAWDOWN_MAX, 0)} risk limit")
        underwater = -(1 - h.growth_index / h.growth_index.cummax()) * 100
        fig = go.Figure(go.Scatter(
            x=h.record_date, y=underwater, mode="lines", line=dict(color=ui.CRITICAL, width=1.5),
            fill="tozeroy", fillcolor="rgba(208,59,59,0.15)",
            hovertemplate="%{x|%b %d}: %{y:.1f}% from peak<extra></extra>",
        ))
        fig.add_hline(y=-ui.DRAWDOWN_MAX * 100, line=dict(color=ui.CRITICAL, width=1, dash="dot"),
                      annotation_text="limit", annotation_font_color=ui.INK_2,
                      annotation_position="bottom left")
        fig.update_yaxes(ticksuffix="%", range=[min(underwater.min(), -ui.DRAWDOWN_MAX * 100) * 1.15, 0.5])
        ui.show(ui.style_fig(fig, height=260, legend=False))

# ---- Daily P&L + monthly returns ---------------------------------------------

left, right = st.columns([7, 5], gap="medium")

with left:
    with st.container(border=True):
        recent = full.tail(30)
        ui.card_header("Daily P&L", "Last 30 trading days")
        fig = go.Figure(go.Bar(
            x=recent.record_date, y=recent.daily_PNL,
            marker=dict(color=[ui.GOOD if v >= 0 else ui.CRITICAL for v in recent.daily_PNL], cornerradius=3),
            hovertemplate="%{x|%b %d}: $%{y:,.0f}<extra></extra>",
        ))
        fig.update_yaxes(tickprefix="$", tickformat="~s")
        fig = ui.style_fig(fig, height=260, legend=False)
        fig.update_layout(bargap=0.35)
        ui.show(fig)
        wins = (recent.daily_PNL > 0).sum()
        best = ui.money(recent.daily_PNL.max(), sign=True).replace("$", r"\$")
        worst = ui.money(recent.daily_PNL.min(), sign=True).replace("$", r"\$")
        st.caption(f"Win rate {wins}/{len(recent)} days ({wins / len(recent):.0%}) · best {best} · worst {worst}")

with right:
    with st.container(border=True):
        ui.card_header("Monthly returns", "Strategy vs. benchmark by calendar month")
        monthly = (full.set_index("record_date")[["growth_index", "benchmark_close"]]
                   .resample("ME").last())
        start = full.set_index("record_date")[["growth_index", "benchmark_close"]].iloc[0]
        rets = monthly.pct_change()
        rets.iloc[0] = monthly.iloc[0] / start - 1
        table = pd.DataFrame({
            "Month": monthly.index.strftime("%b %Y"),
            "Strategy": rets.growth_index.values * 100,
            "Benchmark": rets.benchmark_close.values * 100,
        })
        table["Excess"] = table.Strategy - table.Benchmark
        st.dataframe(
            table.iloc[::-1], hide_index=True, use_container_width=True, height=320,
            column_config={c: st.column_config.NumberColumn(c, format="%+.1f%%")
                           for c in ("Strategy", "Benchmark", "Excess")},
        )

# ---- How this strategy was backtested --------------------------------------------

bt, bt_err = ui.api_get(f"/quant/strategies/{strategy_id}/backtest")
with st.container(border=True):
    ui.card_header("How this strategy is backtested",
                   "Rules run on real daily prices from the strategy's start date; no look-ahead")
    if bt_err:
        st.info("Showing demo data. Refresh market data on the Home page to backtest this strategy on real prices.")
    else:
        left, right = st.columns([3, 2], gap="medium")
        with left:
            hedge = f" · hedge/benchmark leg: **{bt['hedge_ticker']}**" if bt.get("hedge_ticker") else ""
            st.markdown(f"**{bt['engine']}** on **{bt['ticker']}**{hedge}")
            st.markdown(ui.describe_rules(bt["engine"], bt["params_used"]))
            st.markdown(f"Parameters used: `{bt['params_used']}`")
            if bt.get("params_note"):
                st.markdown(ui.badge("Parameter mismatch", "warning") +
                            f'<span class="qt-card-sub">&nbsp; {bt["params_note"]}</span>', unsafe_allow_html=True)
            st.caption(f"Idle cash earns the 3-month T-bill rate. Each trade pays {float(bt['cost_bps']):g} bps "
                       f"of the amount traded. Risk metrics use the trailing {bt['metrics_window_days']} trading days.")
        with right:
            beat = float(bt["total_return"]) >= float(bt["buy_hold_return"])
            st.markdown("".join([
                '<div class="qt-kpis">',
                ui.kpi("Strategy since start", ui.pct(float(bt["total_return"]), 1, sign=True),
                       note=f"CAGR {ui.pct(float(bt['cagr']), 1, sign=True)}" if bt.get("cagr") is not None
                       else f"{bt['trading_days']} trading days"),
                ui.kpi(f"Buy and hold {bt['ticker']}", ui.pct(float(bt["buy_hold_return"]), 1, sign=True),
                       delta="Rules beat holding" if beat else "Holding did better", delta_good=beat),
                '</div><div class="qt-kpis">',
                ui.kpi("Trades", f"{bt['trades_count']}", f"turnover {float(bt['turnover']):.1f}x"),
                ui.kpi("Started", date.fromisoformat(bt["start_date"]).strftime("%b %d, %Y"),
                       f"{ui.money(float(bt['initial_capital']))} initial"
                       + (f" + {ui.money(float(bt['contributions']))} added" if float(bt["contributions"]) else "")),
                '</div>',
            ]), unsafe_allow_html=True)

# ---- Manage strategies --------------------------------------------------------

with st.container(border=True):
    ui.card_header("Manage strategies")
    ui.show_flash()
    tune, create = st.tabs(["Tune this strategy", "Create a strategy"])

    with tune:
        c1, c2 = st.columns([3, 2])
        with c1:
            with st.form("tune_form", border=False):
                new_parameter = st.text_input("Parameters", value=s.parameter,
                                              help="Comma-separated settings, e.g. lookback=30,stop=0.03")
                if st.form_submit_button("Save parameters", type="primary"):
                    ui.write_and_refresh("PUT", f"/strategies/{strategy_id}", {"parameter": new_parameter},
                                         success="Parameters saved.")
        with c2:
            is_active = s.status.lower() == "active"
            st.write("")
            st.markdown(f"Currently **{'active' if is_active else 'inactive'}**.")
            if st.button("Pause strategy" if is_active else "Resume strategy", use_container_width=True):
                ui.write_and_refresh("PUT", f"/strategies/{strategy_id}",
                                     {"status": "inactive" if is_active else "active"},
                                     success=f"{s.strategy_name} {'paused' if is_active else 'resumed'}.")

    with create:
        portfolios, _ = ui.api_get(f"/portfolios/user/{st.session_state.get('user_id', 1)}")
        trades, _ = ui.api_get("/trades/")
        with st.form("create_strategy_form", clear_on_submit=True, border=False):
            a, b = st.columns(2)
            name = a.text_input("Strategy name", placeholder="e.g. Semiconductor Momentum")
            stype = b.selectbox("Type", sorted(scorecard.strategy_type.unique()))
            parameter = a.text_input("Parameters", placeholder="lookback=20,stop=0.02")
            status = b.segmented_control("Starting status", ["active", "inactive"], default="inactive")
            port_labels = {p["portfolio_id"]: f'{p["portfolio_name"]} · #{p["portfolio_id"]}' for p in portfolios or []}
            port = a.selectbox("Portfolio", list(port_labels), format_func=port_labels.get)
            trade_labels = {t["trade_id"]: f'#{t["trade_id"]} · {t["trade_type"]} {t.get("ticker") or ""}'
                            for t in trades or []}
            trade = b.selectbox("Seed trade", list(trade_labels), format_func=trade_labels.get,
                                help="Every strategy is linked to the trade that opened it")
            if st.form_submit_button("Create strategy", type="primary"):
                if not name.strip():
                    st.error("Give the strategy a name.")
                else:
                    ui.write_and_refresh("POST", "/strategies/", {
                        "strategy_name": name.strip(), "strategy_type": stype, "parameter": parameter,
                        "status": status or "inactive", "trade_strat": int(trade), "port_strat": int(port),
                    }, success="Created strategy #{strategy_id}.")

with st.expander("View daily data"):
    st.dataframe(full.iloc[::-1], hide_index=True, use_container_width=True, column_config={
        "record_date": st.column_config.DateColumn("Date"),
        "port_value": st.column_config.NumberColumn("Value ($)", format=ui.MONEY),
        "daily_PNL": st.column_config.NumberColumn("Daily P&L ($)", format=ui.MONEY),
        "benchmark_close": st.column_config.NumberColumn(f"{s.benchmark_ticker} close", format="%.2f"),
    })
