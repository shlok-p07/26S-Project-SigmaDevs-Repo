"""
Strategy engines and the backtest simulator.

Each engine turns daily prices into target weights decided at the close of each
day, using only data available up to that close. The simulator applies those
weights to the next day's returns, so there is no look-ahead. Idle cash earns
the T-bill rate, and every change in weights pays a trading cost.

Engines by strategy type (standard parameters in parentheses):
  Momentum        Long while the N-day return is positive; a trailing stop exits,
                  and re-entry waits until momentum resets.   (lookback=30, stop=0.03)
  Mean Reversion  Buy when price falls k standard deviations below its moving
                  average; sell when it recovers to the average.   (zscore=1.5, window=20)
  Rotation        Every R days, hold up to `max` in the asset if it beat the
                  benchmark over 3 months; the rest in the benchmark.   (rebalance=10, max=0.35)
  Hedging         Hold the asset and short the S&P 500 by its 60-day beta,
                  rebalanced weekly when the hedge drifts past the band.   (hedge_band=0.02)
  Long Only       Buy and hold, adding a fixed contribution each month.   (monthly_contrib=500)
  Passive         Buy and hold.   (hold_forever=true)
"""

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252
COST_BPS = 5.0            # cost per unit of turnover, in basis points
RELATIVE_LOOKBACK = 63    # rotation: 3-month relative strength
BETA_WINDOW = 60          # hedging: rolling beta window
HEDGE_EVERY = 5           # hedging: check the hedge weekly
METRICS_WINDOW = 252      # risk metrics use the trailing 12 months

STANDARD_PARAMS = {
    "Momentum": {"lookback": 30, "stop": 0.03},
    "Mean Reversion": {"zscore": 1.5, "window": 20},
    "Rotation": {"rebalance": 10, "max": 0.35},
    "Hedging": {"hedge_band": 0.02},
    "Long Only": {"monthly_contrib": 500},
    "Passive": {"hold_forever": True},
}


def parse_params(text):
    """'lookback=30,stop=0.03,weekly' -> {'lookback': 30.0, 'stop': 0.03, 'weekly': True}"""
    params = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        key, _, value = part.partition("=")
        if not value:
            params[key.strip()] = True
            continue
        value = value.strip()
        if value.lower() in ("true", "false"):
            params[key.strip()] = value.lower() == "true"
        else:
            try:
                params[key.strip()] = float(value)
            except ValueError:
                params[key.strip()] = value
    return params


def resolve_params(strategy_type, text):
    """Use the stored parameters that belong to this engine; fall back to its standard ones."""
    standard = STANDARD_PARAMS.get(strategy_type, STANDARD_PARAMS["Passive"])
    stored = parse_params(text)
    used = {k: stored.get(k, v) for k, v in standard.items()}
    missing = [k for k in standard if k not in stored]
    note = None
    if missing:
        note = (f"Stored parameters '{text}' don't apply to {strategy_type}; "
                f"used the standard {', '.join(f'{k}={v}' for k, v in standard.items())}")
    return used, note


# ---- Engines: prices -> target weights (asset, hedge) decided at each close ----

def _momentum(px, p):
    lookback, stop = int(p["lookback"]), float(p["stop"])
    signal = (px.asset / px.asset.shift(lookback) - 1).values
    price = px.asset.values
    w = np.zeros(len(px))
    holding, stopped, peak = False, False, 0.0
    for i in range(len(px)):
        s = signal[i]
        if np.isnan(s):
            continue
        if s <= 0:
            holding, stopped = False, False
        elif holding:
            peak = max(peak, price[i])
            if price[i] < peak * (1 - stop):
                holding, stopped = False, True
        elif not stopped:
            holding, peak = True, price[i]
        w[i] = 1.0 if holding else 0.0
    return w, np.zeros(len(px))


def _mean_reversion(px, p):
    window, k = int(p["window"]), float(p["zscore"])
    mean = px.asset.rolling(window).mean()
    sd = px.asset.rolling(window).std()
    z = ((px.asset - mean) / sd).values
    w = np.zeros(len(px))
    holding = False
    for i in range(len(px)):
        if np.isnan(z[i]):
            continue
        if not holding and z[i] < -k:
            holding = True
        elif holding and z[i] >= 0:
            holding = False
        w[i] = 1.0 if holding else 0.0
    return w, np.zeros(len(px))


def _rotation(px, p, start):
    every, cap = max(int(p["rebalance"]), 1), float(p["max"])
    rel = ((px.asset / px.asset.shift(RELATIVE_LOOKBACK))
           - (px.hedge / px.hedge.shift(RELATIVE_LOOKBACK))).values
    wa, wh = np.zeros(len(px)), np.zeros(len(px))
    current = (0.0, 1.0)
    for i in range(len(px)):
        if i >= start and (i - start) % every == 0 and not np.isnan(rel[i]):
            current = (cap, 1 - cap) if rel[i] > 0 else (0.0, 1.0)
        wa[i], wh[i] = current
    return wa, wh


def _hedging(px, p, start):
    band = float(p["hedge_band"])
    ra, rh = px.asset.pct_change(), px.hedge.pct_change()
    beta = (ra.rolling(BETA_WINDOW).cov(rh) / rh.rolling(BETA_WINDOW).var()).clip(0, 2).values
    wa, wh = np.ones(len(px)), np.zeros(len(px))
    hedge = 0.0
    for i in range(len(px)):
        if i >= start and (i - start) % HEDGE_EVERY == 0 and not np.isnan(beta[i]):
            target = -beta[i]
            if abs(target - hedge) > band:
                hedge = target
        wh[i] = hedge
    return wa, wh


def _buy_hold(px, p):
    return np.ones(len(px)), np.zeros(len(px))


def target_weights(strategy_type, px, params, start):
    if strategy_type == "Momentum":
        return _momentum(px, params)
    if strategy_type == "Mean Reversion":
        return _mean_reversion(px, params)
    if strategy_type == "Rotation":
        return _rotation(px, params, start)
    if strategy_type == "Hedging":
        return _hedging(px, params, start)
    return _buy_hold(px, params)  # Long Only, Passive, unknown types


# ---- Simulator ----------------------------------------------------------------

def backtest(strategy_type, px, params, inception, initial_capital, cost_bps=COST_BPS):
    """
    px: DataFrame indexed by date with columns asset, hedge (adjusted closes) and
        rf (daily risk-free return), including warm-up history before inception.
    Returns (daily DataFrame, summary dict), or (None, reason) if there isn't enough data.
    """
    px = px.dropna(subset=["asset", "hedge"])
    inception = pd.Timestamp(inception)
    dates = list(px.index)
    start = next((i for i, d in enumerate(dates) if d >= inception), None)
    if start is None or len(dates) - start < 2:
        return None, "not enough market data after the strategy's start date"

    wa, wh = target_weights(strategy_type, px, params, start)
    wa, wh = wa.copy(), wh.copy()
    wa[:start], wh[:start] = 0.0, 0.0            # nothing is held before inception

    ra = px.asset.pct_change().fillna(0).values
    rh = px.hedge.pct_change().fillna(0).values
    rf = px.rf.fillna(0).values
    cost = cost_bps / 10_000
    contrib_amount = float(params.get("monthly_contrib", 0)) if strategy_type == "Long Only" else 0.0

    rows, equity = [], float(initial_capital)
    turnover_total, trades, contributions = 0.0, 0, 0.0
    for i in range(start, len(dates)):
        if i == start:
            ret = 0.0
        else:
            cash = 1 - wa[i - 1] - wh[i - 1]
            ret = wa[i - 1] * ra[i] + wh[i - 1] * rh[i] + cash * rf[i]
        turnover = abs(wa[i] - (wa[i - 1] if i > start else 0)) + abs(wh[i] - (wh[i - 1] if i > start else 0))
        ret -= turnover * cost
        if turnover > 1e-9:
            trades += 1
            turnover_total += turnover
        contrib = 0.0
        if contrib_amount and i > start and dates[i].month != dates[i - 1].month:
            contrib = contrib_amount
            contributions += contrib
        previous = equity
        equity = equity * (1 + ret) + contrib
        rows.append((dates[i], equity, None if i == start else equity - previous - contrib, ret))

    daily = pd.DataFrame(rows, columns=["date", "equity", "daily_pnl", "ret"]).set_index("date")
    rets = daily.ret.iloc[1:]
    n = len(rets)
    total_return = float((1 + rets).prod() - 1)
    window = rets.tail(METRICS_WINDOW)
    rf_window = pd.Series(rf[start + 1:], index=rets.index).tail(METRICS_WINDOW)
    growth = pd.concat([pd.Series([1.0]), (1 + window).cumprod().reset_index(drop=True)])
    std = window.std()
    summary = {
        "start_date": daily.index[0],
        "end_date": daily.index[-1],
        "trading_days": n,
        "equity": float(daily.equity.iloc[-1]),
        "daily_pnl": float(daily.daily_pnl.iloc[-1] or 0),
        "cumulative_pnl": float(daily.equity.iloc[-1] - initial_capital - contributions),
        "contributions": contributions,
        "trades": trades,
        "turnover": turnover_total,
        "total_return": total_return,
        "cagr": (1 + total_return) ** (TRADING_DAYS / n) - 1 if n >= 60 else None,
        "buy_hold_return": float(px.asset.iloc[-1] / px.asset.iloc[start] - 1),
        "sharpe": float((window - rf_window).mean() / std * math.sqrt(TRADING_DAYS)) if std > 0 else 0.0,
        "volatility": float(std * math.sqrt(TRADING_DAYS)) if n > 1 else 0.0,
        "drawdown": float((1 - growth / growth.cummax()).max()) if n else 0.0,
        "metrics_window": len(window),
    }
    return daily, summary
