# Shared building blocks for the Quant Trader pages: API access with caching,
# risk rules, performance analytics, formatting, KPI cards, and chart styling.

import math
import time
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

API = "http://web-api:4000"

# ---- Design tokens (validated dark palette) ----------------------------------

SURFACE = "#1a1a19"
INK = "#ffffff"
INK_2 = "#c3c2b7"
MUTED = "#898781"
GRID = "#2c2c2a"
AXIS = "#383835"

SERIES_1 = "#3987e5"   # strategy / primary series
SERIES_2 = "#d95926"   # second series (e.g. sells)
BENCHMARK = MUTED      # comparison series is recessive

GOOD = "#0ca30c"
WARNING = "#fab219"
CRITICAL = "#d03b3b"

STATUS_COLOR = {"good": GOOD, "warning": WARNING, "critical": CRITICAL}
STATUS_ICON = {"good": "✓", "warning": "!", "critical": "✕"}

# ---- Risk rules ---------------------------------------------------------------

SHARPE_MIN = 1.0
VOL_MAX = 0.15
DRAWDOWN_MAX = 0.10


def verdict(sharpe, vol, drawdown):
    """Overall risk verdict for a strategy; mirrors SQL query Q1."""
    if sharpe is None or pd.isna(sharpe):
        return "Awaiting data", "warning"
    if sharpe >= SHARPE_MIN and vol <= VOL_MAX and drawdown <= DRAWDOWN_MAX:
        return "Healthy", "good"
    if drawdown > DRAWDOWN_MAX:
        return "Review: drawdown", "critical"
    if sharpe < SHARPE_MIN:
        return "Review: Sharpe", "critical"
    return "Watch: volatility", "warning"


VERDICT_ICON = {"good": "✅", "warning": "⚠️", "critical": "🔴"}


def add_verdicts(df):
    out = df.copy()
    pairs = [verdict(r.sharpe_ratio, r.volatility, r.drawdown) for r in out.itertuples()]
    out["verdict"] = [p[0] for p in pairs]
    out["verdict_status"] = [p[1] for p in pairs]
    out["verdict_label"] = [f"{VERDICT_ICON[s]} {v}" for v, s in pairs]
    return out


# ---- API access ---------------------------------------------------------------

@st.cache_data(ttl=120, show_spinner=False)
def _cached_get(path, params_items):
    res = requests.get(f"{API}{path}", params=dict(params_items), timeout=10)
    return res.status_code, res.json()


def api_get(path, **params):
    """GET with a 2-minute cache. Returns (data, error_message)."""
    try:
        status, data = _cached_get(path, tuple(sorted(params.items())))
    except Exception as e:  # network error, bad JSON
        return None, f"Could not reach the API ({e.__class__.__name__})."
    if status != 200:
        return None, data.get("error", "API error") if isinstance(data, dict) else "API error"
    return data, None


def api_write(method, path, payload=None):
    """POST/PUT/DELETE, then clear cached reads so every page shows fresh data."""
    try:
        res = requests.request(method, f"{API}{path}", json=payload, timeout=10)
        body = res.json()
    except Exception as e:
        return False, f"Could not reach the API ({e.__class__.__name__})."
    _cached_get.clear()
    if res.status_code in (200, 201, 202):   # 202: accepted, e.g. a refresh started in the background
        return True, body
    return False, body.get("error", "Request failed") if isinstance(body, dict) else "Request failed"


def write_and_refresh(method, path, payload=None, success=""):
    """Run a write; on success, queue `success` (a format string over the response) and rerun."""
    ok, res = api_write(method, path, payload)
    if ok:
        st.session_state["qt_flash"] = success.format(**res) if success else "Saved."
        st.session_state["qt_version"] = st.session_state.get("qt_version", 0) + 1
        st.rerun()
    st.error(res)


def widget_key(name):
    """Key that changes after every write, so pickers reset to the fresh list."""
    return f"{name}_{st.session_state.get('qt_version', 0)}"


def show_flash():
    """Show the message queued by write_and_refresh on the previous run.

    The slot is created on every run, even when empty, so the elements after it
    (such as tabs) keep their position and don't reset when the message goes away.
    """
    slot = st.empty()
    message = st.session_state.pop("qt_flash", None)
    if message:
        slot.success(message)


def to_frame(rows, numeric=()):
    df = pd.DataFrame(rows)
    for col in numeric:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


SCORECARD_NUMERIC = ("sharpe_ratio", "volatility", "drawdown", "port_value", "daily_PNL", "cumulative_PNL")


def load_scorecard():
    """Desk-wide strategy scorecard with verdicts (one cached call)."""
    rows, err = api_get("/quant/scorecard")
    if err:
        return None, err
    return add_verdicts(to_frame(rows, SCORECARD_NUMERIC)), None


def my_portfolio_ids():
    rows, _ = api_get(f"/portfolios/user/{st.session_state.get('user_id', 1)}")
    return [r["portfolio_id"] for r in rows or []]


# ---- Performance analytics ---------------------------------------------------

def perf_stats(values, benchmark=None):
    """Return, risk, and relative metrics for a daily value series."""
    values = pd.Series(values, dtype=float).reset_index(drop=True)
    rets = values.pct_change().dropna()
    stats = {
        "return": values.iloc[-1] / values.iloc[0] - 1,
        "vol": rets.std() * math.sqrt(252) if len(rets) > 1 else float("nan"),
        "max_dd": (1 - values / values.cummax()).max(),
        "best_day": rets.max(),
        "worst_day": rets.min(),
        "sharpe": (rets.mean() / rets.std() * math.sqrt(252)) if rets.std() else float("nan"),
    }
    if benchmark is not None:
        bench = pd.Series(benchmark, dtype=float).reset_index(drop=True)
        b_rets = bench.pct_change().dropna()
        stats["bench_return"] = bench.iloc[-1] / bench.iloc[0] - 1
        stats["excess"] = stats["return"] - stats["bench_return"]
        stats["beta"] = rets.cov(b_rets) / b_rets.var() if b_rets.var() else float("nan")
        stats["correlation"] = rets.corr(b_rets)
        active = rets - b_rets
        stats["info_ratio"] = (active.mean() / active.std() * math.sqrt(252)) if active.std() else float("nan")
    return stats


RANGE_DAYS = {"1M": 21, "3M": 63, "6M": 126, "1Y": 252, "Since start": None}


def trim_range(df, label):
    days = RANGE_DAYS[label]
    return (df if days is None else df.tail(days + 1)).reset_index(drop=True)


# ---- Data provenance -----------------------------------------------------------

ENGINE_RULES = {
    "Momentum": "Holds the asset while its {lookback:g}-day return is positive. A {stop:.0%} trailing stop exits; "
                "re-entry waits until momentum resets.",
    "Mean Reversion": "Buys when price falls {zscore:g} standard deviations below its {window:g}-day average; "
                      "sells when it recovers to the average.",
    "Rotation": "Every {rebalance:g} trading days, holds {max:.0%} in the asset if it beat the benchmark over "
                "3 months; the rest (or everything, if it lagged) in the benchmark.",
    "Hedging": "Holds the asset and shorts the S&P 500 by its 60-day beta, checked weekly and rebalanced when "
               "the hedge drifts more than {hedge_band:g}.",
    "Long Only": "Buys and holds, adding ${monthly_contrib:,.0f} at the start of every month.",
    "Passive": "Buys and holds.",
}


def describe_rules(engine, params_used):
    params = {}
    for part in (params_used or "").split(","):
        key, _, value = part.partition("=")
        try:
            params[key] = float(value)
        except ValueError:
            params[key] = value
    try:
        return ENGINE_RULES.get(engine, "Buys and holds.").format(**params)
    except (KeyError, ValueError):
        return ENGINE_RULES.get(engine, "")


STANDARD_PARAMS = {
    "Momentum": "lookback=30,stop=0.03",
    "Mean Reversion": "zscore=1.5,window=20",
    "Rotation": "rebalance=10,max=0.35",
    "Hedging": "hedge_band=0.02",
    "Long Only": "monthly_contrib=500",
    "Passive": "hold_forever=true",
}
SWEEP_VALUES = {
    "Momentum": ("stop", [0.03, 0.05, 0.08, 0.10, 0.15, 0.20]),
    "Mean Reversion": ("zscore", [1.0, 1.25, 1.5, 2.0, 2.5]),
    "Rotation": ("max", [0.2, 0.35, 0.5, 0.75, 1.0]),
    "Hedging": ("hedge_band", [0.01, 0.02, 0.05, 0.1]),
}


def universe_status():
    status, _ = api_get("/quant/universe/status")
    return status or {}


def universe_line(status=None):
    """One-line summary of the whole-market data and the next scheduled update."""
    u = status or universe_status()
    if not u.get("securities"):
        return "Market universe not loaded yet"
    nxt = pd.Timestamp(u["next_scheduled"]).strftime("%a %b %d, %-I:%M %p ET") if u.get("next_scheduled") else "—"
    job = u.get("latest_job") or {}
    if job.get("status") == "running":
        return f"Loading the market universe: {job.get('message', '')} · next scheduled update {nxt}"
    as_of = pd.Timestamp(u["as_of"]).strftime("%b %d") if u.get("as_of") else "—"
    return (f"{u['current']:,} of {u['securities']:,} U.S. stocks and ETFs current through {as_of} · "
            f"updates daily after the close · next {nxt}")


def data_status():
    status, _ = api_get("/quant/data-status")
    return status or {"mode": "demo"}


def data_label(status=None):
    status = status or data_status()
    refresh = status.get("last_successful_refresh") or {}
    if status.get("mode") == "live" and refresh.get("as_of_date"):
        as_of = date.fromisoformat(refresh["as_of_date"]).strftime("%b %d, %Y")
        return f"Real market data (Yahoo Finance, FRED) · as of {as_of}"
    return "Demo data · refresh to load real market data"


def refresh_panel():
    """Data source badge, refresh button, and provenance details."""
    status = data_status()
    live = status.get("mode") == "live"
    refresh = status.get("last_successful_refresh") or {}
    latest = status.get("latest_refresh") or {}
    c1, c2 = st.columns([5, 2])
    with c1:
        if live:
            rf = status.get("risk_free") or {}
            st.markdown(
                badge("Live data", "good") + f'<span class="qt-card-sub">&nbsp; {data_label(status)} · '
                f'{refresh.get("tickers_loaded", 0)} tickers · {refresh.get("strategies_backtested", 0)} strategies '
                f'backtested · T-bill {rf.get("rate_pct", "—")}%</span>', unsafe_allow_html=True)
        else:
            st.markdown(badge("Demo data", "warning") + '<span class="qt-card-sub">&nbsp; Calibrated sample data. '
                        'Refresh to backtest every strategy on real prices.</span>', unsafe_allow_html=True)
        st.caption(universe_line())
        if latest.get("status") == "failed":
            st.caption(f"Last refresh failed: {(latest.get('message') or '').splitlines()[-1][:160]}")
    with c2:
        if st.button("↻ Refresh market data", use_container_width=True):
            ok, res = api_write("POST", "/quant/refresh")
            if not ok and "already running" not in str(res):
                st.error(res)
                return
            with st.spinner("Loading prices and backtesting strategies…"):
                for _ in range(90):
                    time.sleep(2)
                    _cached_get.clear()
                    current = (data_status().get("latest_refresh") or {})
                    if current.get("status") not in ("running", None):
                        break
            st.session_state["qt_version"] = st.session_state.get("qt_version", 0) + 1
            st.rerun()


# ---- Formatting --------------------------------------------------------------

def money(x, compact=True, sign=False):
    if x is None or pd.isna(x):
        return "—"
    prefix = "+" if sign and x > 0 else ("−" if x < 0 else "")
    x = abs(x)
    if compact and x >= 1e9:
        body = f"${x / 1e9:.2f}B"
    elif compact and x >= 1e6:
        body = f"${x / 1e6:.2f}M"
    elif compact and x >= 1e4:
        body = f"${x / 1e3:.1f}K"
    else:
        body = f"${x:,.0f}"
    return prefix + body


def pct(x, digits=1, sign=False):
    if x is None or pd.isna(x):
        return "—"
    s = f"{x * 100:+.{digits}f}%" if sign else f"{x * 100:.{digits}f}%"
    return s.replace("-", "−")


def num(x, digits=2):
    return "—" if x is None or pd.isna(x) else f"{x:.{digits}f}".replace("-", "−")


# ---- Page chrome -------------------------------------------------------------

CSS = f"""
<style>
.block-container {{ padding-top: 3.6rem; padding-bottom: 3rem; max-width: 1400px; }}
.qt-eyebrow {{ color: {MUTED}; font-size: 0.78rem; letter-spacing: 0.08em;
               text-transform: uppercase; margin-bottom: 0.15rem; }}
.qt-title {{ color: {INK}; font-size: 1.9rem; font-weight: 650; line-height: 1.2; margin: 0; }}
.qt-sub {{ color: {INK_2}; font-size: 0.95rem; margin: 0.35rem 0 0.4rem 0; }}
.qt-kpis {{ display: grid; gap: 12px; margin: 0.4rem 0 0.6rem 0;
            grid-template-columns: repeat(auto-fit, minmax(148px, 1fr)); }}
.qt-kpi {{ background: {SURFACE}; border: 1px solid rgba(255,255,255,0.08);
           border-radius: 12px; padding: 14px 16px 12px 16px; }}
.qt-kpi-label {{ color: {MUTED}; font-size: 0.78rem; font-weight: 500; }}
.qt-kpi-value {{ color: {INK}; font-size: 1.45rem; font-weight: 650; margin-top: 4px; line-height: 1.2; }}
.qt-kpi-note {{ color: {INK_2}; font-size: 0.8rem; margin-top: 4px; }}
.qt-delta-good {{ color: {GOOD}; font-weight: 600; }}
.qt-delta-bad {{ color: {CRITICAL}; font-weight: 600; }}
.qt-badge {{ display: inline-flex; align-items: center; gap: 6px; padding: 2px 10px;
             border-radius: 999px; font-size: 0.78rem; font-weight: 600; white-space: nowrap; }}
.qt-badge-dot {{ font-weight: 800; }}
.qt-card-title {{ color: {INK}; font-weight: 600; font-size: 1.02rem; margin: 0; }}
.qt-card-sub {{ color: {MUTED}; font-size: 0.82rem; margin: 2px 0 6px 0; }}
.qt-alert {{ display: flex; justify-content: space-between; align-items: center; gap: 12px;
             padding: 10px 2px; border-bottom: 1px solid {GRID}; }}
.qt-alert:last-child {{ border-bottom: none; }}
.qt-alert-name {{ color: {INK}; font-weight: 600; }}
.qt-alert-meta {{ color: {MUTED}; font-size: 0.8rem; }}
.qt-alert-rule {{ color: {INK_2}; font-size: 0.85rem; text-align: right; }}
.qt-rule {{ background: {SURFACE}; border: 1px solid rgba(255,255,255,0.08); border-radius: 12px;
            padding: 14px 16px; height: 100%; }}
.qt-rule-value {{ color: {INK}; font-size: 1.6rem; font-weight: 650; }}
.qt-rule-limit {{ color: {MUTED}; font-size: 0.8rem; margin: 2px 0 8px 0; }}
.qt-rule-text {{ color: {INK_2}; font-size: 0.85rem; margin-top: 8px; }}
.qt-bar {{ height: 6px; border-radius: 3px; background: {GRID}; position: relative; margin-top: 6px; }}
.qt-bar-fill {{ height: 6px; border-radius: 3px; position: absolute; left: 0; top: 0; }}
.qt-bar-mark {{ position: absolute; top: -4px; width: 2px; height: 14px; background: {INK_2}; }}
</style>
"""


def setup_page(title, subtitle):
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown(
        f'<div class="qt-eyebrow">PortIQ · Quant Desk · {data_label()}</div>'
        f'<div class="qt-title">{title}</div><div class="qt-sub">{subtitle}</div>',
        unsafe_allow_html=True,
    )


def badge(text, status):
    color = STATUS_COLOR[status]
    return (
        f'<span class="qt-badge" style="background:{color}22;color:{INK};border:1px solid {color}66">'
        f'<span class="qt-badge-dot" style="color:{color}">{STATUS_ICON[status]}</span>{text}</span>'
    )


def kpi(label, value, note=None, delta=None, delta_good=None):
    """One KPI card. `delta` text is colored by `delta_good` (True/False/None)."""
    delta_html = ""
    if delta is not None:
        cls = "" if delta_good is None else ("qt-delta-good" if delta_good else "qt-delta-bad")
        delta_html = f'<span class="{cls}">{delta}</span> '
    note_html = f'<div class="qt-kpi-note">{delta_html}{note or ""}</div>' if (note or delta) else ""
    return (
        f'<div class="qt-kpi"><div class="qt-kpi-label">{label}</div>'
        f'<div class="qt-kpi-value">{value}</div>{note_html}</div>'
    )


def kpi_row(cards):
    st.markdown(f'<div class="qt-kpis">{"".join(cards)}</div>', unsafe_allow_html=True)


def card_header(title, subtitle=None):
    sub = f'<div class="qt-card-sub">{subtitle}</div>' if subtitle else ""
    st.markdown(f'<div class="qt-card-title">{title}</div>{sub}', unsafe_allow_html=True)


def rule_card(name, value_text, limit_text, ok, explanation, fill, marker):
    """Metric vs. threshold card with a bullet bar (fill and marker are 0-1 positions)."""
    status = "good" if ok else "critical"
    color = STATUS_COLOR[status]
    return (
        f'<div class="qt-rule"><div class="qt-kpi-label">{name}</div>'
        f'<div class="qt-rule-value">{value_text}</div>'
        f'<div class="qt-rule-limit">{limit_text}</div>'
        f'{badge("Within limit" if ok else "Outside limit", status)}'
        f'<div class="qt-bar"><div class="qt-bar-fill" style="width:{min(fill, 1) * 100:.0f}%;background:{color}"></div>'
        f'<div class="qt-bar-mark" style="left:{min(marker, 1) * 100:.0f}%"></div></div>'
        f'<div class="qt-rule-text">{explanation}</div></div>'
    )


# ---- Charts ----------------------------------------------------------------

PLOTLY_CONFIG = {"displayModeBar": False}


def style_fig(fig, height=340, legend=True):
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=8 if not legend else 36, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="system-ui, -apple-system, Segoe UI, sans-serif", color=INK_2, size=12),
        hoverlabel=dict(bgcolor="#262624", bordercolor=AXIS, font=dict(color=INK, size=12)),
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0,
                    font=dict(color=INK_2), bgcolor="rgba(0,0,0,0)"),
        bargap=0.25,
    )
    fig.update_xaxes(showgrid=False, linecolor=AXIS, tickfont=dict(color=MUTED), zeroline=False)
    fig.update_yaxes(gridcolor=GRID, linecolor=AXIS, tickfont=dict(color=MUTED),
                     zeroline=False, tickfont_family="system-ui")
    return fig


MONEY = "%,.0f"   # pair with a "($)" column header so negatives read cleanly


def show(fig):
    st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)


def indexed_growth_chart(dates, series, height=340):
    """Lines indexed to 100 on one axis. series: list of (name, values, color, width)."""
    fig = go.Figure()
    for name, values, color, width in series:
        values = pd.Series(values, dtype=float).reset_index(drop=True)
        idx = values / values.iloc[0] * 100
        fig.add_trace(go.Scatter(
            x=dates, y=idx, name=name, mode="lines",
            line=dict(color=color, width=width),
            hovertemplate=f"{name}: %{{y:.1f}}<extra></extra>",
        ))
        fig.add_annotation(x=dates.iloc[-1], y=idx.iloc[-1], text=f"{idx.iloc[-1] - 100:+.1f}%",
                           showarrow=False, xanchor="left", xshift=6,
                           font=dict(color=INK, size=12))
    fig.add_hline(y=100, line=dict(color=AXIS, width=1))
    fig.update_layout(hovermode="x unified")
    fig.update_xaxes(range=[dates.iloc[0], dates.iloc[-1]])
    fig.update_yaxes(ticksuffix="", title=None)
    style_fig(fig, height)
    fig.update_layout(margin=dict(r=56))
    return fig
