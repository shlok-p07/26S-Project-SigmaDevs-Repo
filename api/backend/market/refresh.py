"""
Market data refresh: load real prices, then backtest every strategy on them.

Steps (each run is logged in DataRefresh):
  1. Daily prices for every ticker in Asset and Benchmark (Yahoo Finance)
  2. 3-month T-bill rate (FRED), converted to a daily risk-free return
  3. Reference data: correct asset names and types; latest benchmark values
  4. Positions: cost basis = real close on the acquisition date; market value
     and unrealized P&L from the latest close
  5. Trades: seed trades priced more than 25% away from that day's real close
     are re-priced to the close (user-entered trades near the market are kept)
  6. Backtests: run each strategy's rules from its creation date, then write
     StrategyHistory, RiskMetric, PerformanceRecord, and StrategyBacktest

Run from the API container:  python -m backend.market.refresh
"""

import logging
import threading
from datetime import date, datetime, timedelta

import mysql.connector
import pandas as pd

from backend.market import providers, strategies

log = logging.getLogger("market.refresh")

WARMUP_DAYS = 200           # calendar days of history before the earliest start date
REPRICE_TOLERANCE = 0.25    # re-price trades further than this from the day's close
DEFAULT_CAPITAL = 100_000
_lock = threading.Lock()


def _connect(config):
    return mysql.connector.connect(
        host=config["MYSQL_DATABASE_HOST"], user=config["MYSQL_DATABASE_USER"],
        password=config["MYSQL_DATABASE_PASSWORD"], database=config["MYSQL_DATABASE_DB"],
        port=config["MYSQL_DATABASE_PORT"],
    )


def _rows(cur, query, params=()):
    cur.execute(query, params)
    return cur.fetchall()


def _close_on(prices, day):
    """Latest close on or before `day` (falls back to the first available close)."""
    day = pd.Timestamp(day)
    frame = prices.loc[:day] if day >= prices.index[0] else prices.iloc[:1]
    return float(frame.close.iloc[-1])


def upsert_prices(cur, ticker, frame, batch=2000):
    """Insert or update daily prices (the full adjusted history changes after splits and dividends)."""
    rows = [(ticker, d.date(), round(float(r.close), 4), round(float(r.adj_close), 4),
             None if pd.isna(r.volume) else int(r.volume)) for d, r in frame.iterrows()]
    for i in range(0, len(rows), batch):
        cur.executemany(
            "INSERT INTO MarketPrice (ticker, price_date, close_value, adj_close, volume, source) "
            "VALUES (%s, %s, %s, %s, %s, 'Yahoo') ON DUPLICATE KEY UPDATE close_value = VALUES(close_value), "
            "adj_close = VALUES(adj_close), volume = VALUES(volume), source = 'Yahoo'", rows[i:i + batch])


def run_refresh(conn):
    cur = conn.cursor(dictionary=True)
    # Database-level lock: the scheduler container and the API button can't overlap
    cur.execute("SELECT GET_LOCK('portiq_core_refresh', 0) AS got")
    if not cur.fetchone()["got"]:
        cur.close()
        return {"status": "skipped", "reason": "a core refresh is already running"}
    try:
        return _run_core(conn, cur)
    finally:
        cur.execute("DO RELEASE_LOCK('portiq_core_refresh')")
        cur.close()


def _run_core(conn, cur):
    started = datetime.now()
    cur.execute("INSERT INTO DataRefresh (started_at, job, status, source) VALUES (%s, 'core', 'running', %s)",
                (started, "Yahoo Finance + FRED"))
    refresh_id = cur.lastrowid
    conn.commit()
    notes, failed = [], []

    try:
        # ---- 1. Prices -------------------------------------------------------
        tickers = sorted({r["ticker"] for r in _rows(cur, "SELECT DISTINCT ticker FROM Asset")}
                         | {r["ticker"] for r in _rows(cur, "SELECT DISTINCT ticker FROM Benchmark")}
                         | {"SPY"})
        earliest = _rows(cur, """
            SELECT LEAST(COALESCE((SELECT MIN(created_at) FROM Strategy), NOW()),
                         COALESCE((SELECT MIN(acquired_date) FROM StockPosition), NOW()),
                         COALESCE((SELECT MIN(trade_date) FROM Trade), NOW())) AS d""")[0]["d"]
        fetch_start = (earliest.date() if isinstance(earliest, datetime) else earliest) - timedelta(days=WARMUP_DAYS)

        prices, reference, price_rows = {}, {}, 0
        for t in tickers:
            try:
                frame, ref = providers.fetch_prices(t, fetch_start)
            except Exception as e:
                failed.append(t)
                notes.append(f"{t}: {e}")
                continue
            prices[t], reference[t] = frame, ref
            cur.execute("DELETE FROM MarketPrice WHERE ticker = %s AND source = 'demo'", (t,))
            upsert_prices(cur, t, frame)
            price_rows += len(frame)
        conn.commit()
        if "SPY" not in prices:
            raise RuntimeError("could not load SPY prices; keeping the existing data")
        as_of = prices["SPY"].index[-1].date()

        # ---- 2. Risk-free rate -------------------------------------------------
        try:
            rates = providers.fetch_risk_free(fetch_start)
            cur.execute("DELETE FROM RiskFreeRate")
            cur.executemany("INSERT INTO RiskFreeRate (rate_date, rate_pct, source) VALUES (%s, %s, 'FRED')",
                            [(d.date(), float(v)) for d, v in rates.items()])
            conn.commit()
        except Exception as e:
            rates = pd.Series(dtype=float)
            notes.append(f"Risk-free rate unavailable, using 0%: {e}")
        calendar = prices["SPY"].index
        rf_daily = (rates.reindex(calendar).ffill().bfill().fillna(0) / 100 + 1) ** (1 / 252) - 1 \
            if len(rates) else pd.Series(0.0, index=calendar)

        # ---- 3. Reference data ---------------------------------------------------
        for t, ref in reference.items():
            cur.execute("UPDATE Asset SET asset_name = %s, asset_type = %s WHERE ticker = %s",
                        (ref["name"], ref["asset_type"], t))
            cur.execute("UPDATE Benchmark SET current_value = %s WHERE ticker = %s",
                        (round(float(prices[t].close.iloc[-1]), 2), t))

        # ---- 4. Positions ----------------------------------------------------------
        for pos in _rows(cur, """SELECT sp.position_id, sp.avg_cost, sp.price_target, sp.qty_held,
                                        sp.acquired_date, a.ticker
                                 FROM StockPosition sp JOIN Asset a ON a.pos_id = sp.position_id"""):
            if pos["ticker"] not in prices:
                continue
            px = prices[pos["ticker"]]
            cost = _close_on(px, pos["acquired_date"].date())
            last = float(px.close.iloc[-1])
            ratio = (pos["price_target"] / pos["avg_cost"]) if pos["avg_cost"] else 1.15
            cur.execute("""UPDATE StockPosition SET avg_cost = %s, market_value = %s,
                                  unrealized_PNL = %s, price_target = %s WHERE position_id = %s""",
                        (round(cost, 2), round(last * pos["qty_held"], 2),
                         round((last - cost) * pos["qty_held"], 2), round(cost * ratio, 2), pos["position_id"]))

        # ---- 5. Trades -------------------------------------------------------------
        repriced = 0
        for tr in _rows(cur, """SELECT t.trade_id, t.price, t.trade_date, a.ticker
                                FROM Trade t JOIN Asset a ON a.asset_id = t.trade_asset"""):
            if tr["ticker"] not in prices or tr["trade_date"] is None:
                continue
            close = _close_on(prices[tr["ticker"]], tr["trade_date"].date())
            if abs(float(tr["price"]) / close - 1) > REPRICE_TOLERANCE:
                cur.execute("UPDATE Trade SET price = %s WHERE trade_id = %s", (round(close, 2), tr["trade_id"]))
                repriced += 1
        if repriced:
            notes.append(f"Re-priced {repriced} trades to the day's real close")
        conn.commit()

        # ---- 6. Backtests ------------------------------------------------------------
        strategies_rows = _rows(cur, """
            SELECT s.strategy_id, s.strategy_type, s.parameter, s.created_at, a.ticker,
                   (SELECT MIN(b.ticker) FROM Benchmark b WHERE b.strat_bench = s.strategy_id) AS bench,
                   bt.initial_capital AS saved_capital,
                   (SELECT p.port_value - p.cumulative_PNL FROM PerformanceRecord p
                     WHERE p.strat_perf = s.strategy_id ORDER BY p.performance_id LIMIT 1) AS seed_capital
            FROM Strategy s
            JOIN Trade t ON t.trade_id = s.trade_strat
            JOIN Asset a ON a.asset_id = t.trade_asset
            LEFT JOIN StrategyBacktest bt ON bt.strategy_id = s.strategy_id""")
        next_risk = _rows(cur, "SELECT COALESCE(MAX(riskmetric_id), 0) + 1 AS n FROM RiskMetric")[0]["n"]
        next_perf = _rows(cur, "SELECT COALESCE(MAX(performance_id), 0) + 1 AS n FROM PerformanceRecord")[0]["n"]
        backtested, mismatched = 0, 0

        for s in strategies_rows:
            hedge = s["bench"] if s["strategy_type"] == "Rotation" and s["bench"] in prices else "SPY"
            if s["ticker"] not in prices:
                notes.append(f"Strategy {s['strategy_id']}: no prices for {s['ticker']}")
                continue
            params, note = strategies.resolve_params(s["strategy_type"], s["parameter"])
            mismatched += bool(note)
            px = pd.DataFrame({
                "asset": prices[s["ticker"]].adj_close,
                "hedge": prices[hedge].adj_close,
            }).dropna()
            px["rf"] = rf_daily.reindex(px.index).fillna(0)
            # Rounded like the stored value, so later runs start from exactly the same capital
            capital = round(float(s["saved_capital"] or (s["seed_capital"] if (s["seed_capital"] or 0) > 0
                                                         else DEFAULT_CAPITAL)), 2)
            daily, summary = strategies.backtest(
                s["strategy_type"], px, params, s["created_at"].date(), capital)
            if daily is None:
                notes.append(f"Strategy {s['strategy_id']}: {summary}")
                continue
            sid = s["strategy_id"]

            cur.execute("DELETE FROM StrategyHistory WHERE strategy_id = %s", (sid,))
            cur.executemany(
                "INSERT INTO StrategyHistory (strategy_id, record_date, port_value, daily_PNL) VALUES (%s, %s, %s, %s)",
                [(sid, d.date(), round(r.equity, 2), None if pd.isna(r.daily_pnl) else round(r.daily_pnl, 2))
                 for d, r in daily.iterrows()])

            risk = (round(summary["sharpe"], 4), round(summary["volatility"], 4),
                    round(summary["drawdown"], 4), summary["end_date"].date())
            # Check existence explicitly: MySQL's UPDATE rowcount counts changed rows, not
            # matched rows, so an unchanged row would look missing and get duplicated.
            if _rows(cur, "SELECT 1 FROM RiskMetric WHERE risk_strat = %s LIMIT 1", (sid,)):
                cur.execute("UPDATE RiskMetric SET sharpe_ratio = %s, volatility = %s, drawdown = %s, "
                            "calculated_at = %s WHERE risk_strat = %s", (*risk, sid))
            else:
                cur.execute("INSERT INTO RiskMetric (sharpe_ratio, volatility, drawdown, calculated_at, "
                            "riskmetric_id, risk_strat) VALUES (%s, %s, %s, %s, %s, %s)", (*risk, next_risk, sid))
                next_risk += 1

            perf = (round(summary["equity"], 2), round(summary["cumulative_pnl"], 2),
                    round(summary["daily_pnl"], 2), summary["end_date"].date())
            if _rows(cur, "SELECT 1 FROM PerformanceRecord WHERE strat_perf = %s LIMIT 1", (sid,)):
                cur.execute("UPDATE PerformanceRecord SET port_value = %s, cumulative_PNL = %s, daily_PNL = %s, "
                            "record_date = %s WHERE strat_perf = %s", (*perf, sid))
            else:
                cur.execute("INSERT INTO PerformanceRecord (port_value, cumulative_PNL, daily_PNL, record_date, "
                            "performance_id, strat_perf) VALUES (%s, %s, %s, %s, %s, %s)", (*perf, next_perf, sid))
                next_perf += 1

            cur.execute("""REPLACE INTO StrategyBacktest
                (strategy_id, engine, ticker, hedge_ticker, params_used, params_note, start_date, end_date,
                 trading_days, initial_capital, contributions, trades_count, turnover, cost_bps, total_return,
                 cagr, buy_hold_return, metrics_window_days, run_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", (
                sid, s["strategy_type"], s["ticker"],
                hedge if s["strategy_type"] in ("Rotation", "Hedging") else None,
                ",".join(f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}" for k, v in params.items()),
                note, summary["start_date"].date(), summary["end_date"].date(), summary["trading_days"], round(capital, 2),
                round(summary["contributions"], 2), summary["trades"], round(summary["turnover"], 2),
                strategies.COST_BPS, round(summary["total_return"], 4),
                None if summary["cagr"] is None else round(summary["cagr"], 4),
                round(summary["buy_hold_return"], 4), summary["metrics_window"], datetime.now()))
            backtested += 1
            conn.commit()

        if mismatched:
            notes.append(f"{mismatched} strategies had stored parameters that don't fit their type "
                         "and used their type's standard parameters")
        status = "partial" if failed else "success"
        cur.execute("""UPDATE DataRefresh SET finished_at = %s, status = %s, as_of_date = %s,
                              tickers_loaded = %s, price_rows = %s, strategies_backtested = %s, message = %s
                       WHERE refresh_id = %s""",
                    (datetime.now(), status, as_of, len(prices), price_rows, backtested,
                     "\n".join(notes) or None, refresh_id))
        conn.commit()
        log.info(f"Refresh {refresh_id} {status}: {len(prices)} tickers, {backtested} strategies")
        return {"refresh_id": refresh_id, "status": status}
    except Exception as e:
        conn.rollback()
        cur.execute("UPDATE DataRefresh SET finished_at = %s, status = 'failed', message = %s WHERE refresh_id = %s",
                    (datetime.now(), "\n".join(notes + [str(e)]), refresh_id))
        conn.commit()
        log.exception("Market data refresh failed")
        return {"refresh_id": refresh_id, "status": "failed", "error": str(e)}


def start_background_refresh(config):
    """Start a refresh in a background thread. Returns False if one is already running."""
    if not _lock.acquire(blocking=False):
        return False

    def worker():
        try:
            conn = _connect(config)
            try:
                run_refresh(conn)
            finally:
                conn.close()
        finally:
            _lock.release()

    threading.Thread(target=worker, daemon=True, name="market-refresh").start()
    return True


if __name__ == "__main__":
    from backend.rest_entry import create_app
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    conn = _connect(app.config)
    try:
        print(run_refresh(conn))
    finally:
        conn.close()
