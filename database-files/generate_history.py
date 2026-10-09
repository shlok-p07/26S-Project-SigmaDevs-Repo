"""
Generate 05-demo-history.sql: six months of calibrated demo history for every
strategy and benchmark in 03-mock-data.sql.

This is the offline fallback. With internet access, the market data refresh
(api/backend/market/refresh.py) replaces it with real prices and real backtests.

Each strategy path is calibrated to the seed data so the charts agree with the
risk tables:
  * it ends exactly at the strategy's PerformanceRecord.port_value
  * its final day's P&L equals PerformanceRecord.daily_PNL
  * its maximum drawdown matches RiskMetric.drawdown
  * its six-month return follows from RiskMetric.sharpe_ratio x volatility
  * its daily moves are correlated with its benchmark (by strategy type)

Output is deterministic (fixed seed). Run from this folder:
    python3 generate_history.py
"""

import ast
import math
import random
import re
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
SEED_SQL = HERE / "03-mock-data.sql"
OUT_SQL = HERE / "05-demo-history.sql"

END_DATE = date(2026, 4, 10)   # "as of" date used across the seed data
TRADING_DAYS = 126             # about six months

# Benchmark assumptions: (annualized volatility, six-month return)
BENCHMARK_PARAMS = {
    "SPY": (0.15, 0.052),
    "QQQ": (0.20, 0.071),
    "ACWI": (0.14, 0.041),
    "AGG": (0.05, 0.012),
    "IWM": (0.22, 0.028),
}

# How closely each strategy type tracks its benchmark (correlation of daily moves)
TYPE_CORRELATION = {
    "Momentum": 0.80,
    "Rotation": 0.70,
    "Long Only": 0.90,
    "Passive": 0.95,
    "Mean Reversion": 0.30,
    "Hedging": -0.40,
}


def read_rows(table):
    """Return the value tuples from `INSERT INTO <table> (...) VALUES` in the seed file."""
    text = SEED_SQL.read_text()
    block = re.search(
        rf"INSERT INTO {table} \((.*?)\) VALUES(.*?);\s*$", text, re.S | re.M
    )
    columns = [c.strip() for c in block.group(1).split(",")]
    rows = []
    for line in block.group(2).splitlines():
        line = line.strip().rstrip(",")
        if line.startswith("("):
            values = ast.literal_eval(line.replace("NULL", "None"))
            rows.append(dict(zip(columns, values)))
    return rows


def trading_days(end, n):
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def bridge(shocks, total_log_return, scale):
    """Daily log returns with the given noise scale, shifted so they sum to total_log_return."""
    n = len(shocks)
    mean = sum(shocks) / n
    drift = total_log_return / n
    return [drift + scale * (s - mean) for s in shocks]


def max_drawdown(log_returns):
    level, peak, worst = 0.0, 0.0, 0.0
    for r in log_returns:
        level += r
        peak = max(peak, level)
        worst = max(worst, 1 - math.exp(level - peak))
    return worst


def calibrate_scale(shocks, total_log_return, target_dd):
    """Bisection: find the noise scale whose path has the target maximum drawdown."""
    lo, hi = 0.0, 0.2
    for _ in range(60):
        mid = (lo + hi) / 2
        if max_drawdown(bridge(shocks, total_log_return, mid)) < target_dd:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def main():
    rng = random.Random(3200)
    days = trading_days(END_DATE, TRADING_DAYS)
    n = len(days) - 1  # number of daily returns

    strategies = {r["strategy_id"]: r for r in read_rows("Strategy")}
    risk = {r["risk_strat"]: r for r in read_rows("RiskMetric")}
    perf = {r["strat_perf"]: r for r in read_rows("PerformanceRecord")}
    bench_rows = read_rows("Benchmark")
    bench_for_strategy = {}
    for b in bench_rows:
        bench_for_strategy.setdefault(b["strat_bench"], b["ticker"])

    # Benchmark closing values end at the average current_value recorded for the ticker
    bench_end = {}
    for b in bench_rows:
        bench_end.setdefault(b["ticker"], []).append(float(b["current_value"]))
    bench_end = {t: sum(v) / len(v) for t, v in bench_end.items()}

    bench_shocks, bench_lines = {}, []
    for ticker in sorted(bench_end):
        vol, six_month = BENCHMARK_PARAMS[ticker]
        shocks = [rng.gauss(0, 1) for _ in range(n)]
        mean = sum(shocks) / n
        sd = math.sqrt(sum((s - mean) ** 2 for s in shocks) / n)
        bench_shocks[ticker] = [(s - mean) / sd for s in shocks]
        rets = bridge(bench_shocks[ticker], math.log(1 + six_month), vol / math.sqrt(252))
        level = [0.0]
        for r in rets:
            level.append(level[-1] + r)
        for d, lv in zip(days, level):
            close = bench_end[ticker] * math.exp(lv - level[-1])
            bench_lines.append(f"('{ticker}', '{d}', {close:.4f}, {close:.4f}, 'demo')")

    strat_lines = []
    for sid in sorted(strategies):
        s, rk, pf = strategies[sid], risk[sid], perf[sid]
        rho = TYPE_CORRELATION.get(s["strategy_type"], 0.5)
        market = bench_shocks[bench_for_strategy[sid]]
        shocks = [rho * m + math.sqrt(1 - rho ** 2) * rng.gauss(0, 1) for m in market]

        total = rk["sharpe_ratio"] * rk["volatility"] * n / 252
        scale = calibrate_scale(shocks, total, rk["drawdown"])
        rets = bridge(shocks, total, scale)

        level = [0.0]
        for r in rets:
            level.append(level[-1] + r)
        end_value = float(pf["port_value"])
        values = [end_value * math.exp(lv - level[-1]) for lv in level]
        values[-2] = end_value - float(pf["daily_PNL"])  # final day P&L matches the snapshot

        for i, (d, v) in enumerate(zip(days, values)):
            pnl = "NULL" if i == 0 else f"{v - values[i - 1]:.2f}"
            strat_lines.append(f"({sid}, '{d}', {v:.2f}, {pnl})")

    def chunks(lines, size=500):
        for i in range(0, len(lines), size):
            yield lines[i:i + size]

    out = [
        "-- Generated by generate_history.py. Do not edit by hand.",
        f"-- Demo fallback: {TRADING_DAYS} trading days ending {END_DATE}. Tables are in 04-market-data-ddl.sql.",
        "USE PortIQ;",
        "",
    ]
    for part in chunks(bench_lines):
        out.append("INSERT INTO MarketPrice (ticker, price_date, close_value, adj_close, source) VALUES")
        out.append(",\n".join(part) + ";")
    for part in chunks(strat_lines):
        out.append("INSERT INTO StrategyHistory (strategy_id, record_date, port_value, daily_PNL) VALUES")
        out.append(",\n".join(part) + ";")
    OUT_SQL.write_text("\n".join(out) + "\n")
    print(f"Wrote {OUT_SQL.name}: {len(bench_lines)} benchmark rows, {len(strat_lines)} strategy rows")


if __name__ == "__main__":
    main()
