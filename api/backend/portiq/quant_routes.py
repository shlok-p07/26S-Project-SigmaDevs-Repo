from flask import Blueprint, jsonify, request, current_app
from backend.db_connection import get_db
from backend.market.refresh import start_background_refresh
from mysql.connector import Error

# Analytics endpoints behind the redesigned Quant Trader pages.
# Each one returns everything a page section needs in a single call.
quant_routes = Blueprint("quant_routes", __name__)

SPARKLINE_DAYS = 30


def _growth_index(values, pnls):
    """Time-weighted growth index (starts at 100): chains daily P&L / prior value, so
    contributions and newly started strategies don't show up as returns."""
    index, level, prev = [], 100.0, None
    for value, pnl in zip(values, pnls):
        if prev and pnl is not None:
            level *= 1 + float(pnl) / prev
        index.append(round(level, 6))
        prev = float(value) if value is not None else prev
    return index


def _run(query, params=()):
    cursor = get_db().cursor(dictionary=True)
    try:
        cursor.execute(query, params)
        return cursor.fetchall()
    finally:
        cursor.close()


# GET /quant/scorecard?portfolio_id=<id>
# One row per strategy: latest performance, risk metrics, benchmark, and a
# 30-day sparkline. Without portfolio_id, returns the whole desk.
@quant_routes.route("/scorecard", methods=["GET"])
def get_scorecard():
    portfolio_id = request.args.get("portfolio_id", type=int)
    try:
        current_app.logger.info(f"GET /quant/scorecard portfolio_id={portfolio_id}")
        where = "WHERE s.port_strat = %s" if portfolio_id else ""
        params = (portfolio_id,) if portfolio_id else ()
        rows = _run(
            f"""
            SELECT s.strategy_id, s.strategy_name, s.strategy_type, s.status, s.parameter,
                   s.port_strat AS portfolio_id, pf.portfolio_name,
                   r.sharpe_ratio, r.volatility, r.drawdown,
                   p.port_value, p.daily_PNL, p.cumulative_PNL,
                   b.ticker AS benchmark_ticker, b.benchmark_name
            FROM Strategy s
            JOIN Portfolio pf              ON pf.portfolio_id = s.port_strat
            LEFT JOIN RiskMetric r         ON r.risk_strat = s.strategy_id
            LEFT JOIN PerformanceRecord p  ON p.strat_perf = s.strategy_id
            LEFT JOIN Benchmark b          ON b.strat_bench = s.strategy_id
            {where}
            ORDER BY s.strategy_id
            """,
            params,
        )
        if not rows:
            return jsonify([]), 200

        ids = [r["strategy_id"] for r in rows]
        placeholders = ",".join(["%s"] * len(ids))
        history = _run(
            f"""
            SELECT strategy_id, port_value
            FROM StrategyHistory
            WHERE strategy_id IN ({placeholders})
              AND record_date >= (SELECT MAX(record_date) FROM StrategyHistory)
                                 - INTERVAL {SPARKLINE_DAYS + 14} DAY
            ORDER BY strategy_id, record_date
            """,
            tuple(ids),
        )
        sparks = {}
        for h in history:
            sparks.setdefault(h["strategy_id"], []).append(float(h["port_value"]))
        for r in rows:
            r["sparkline"] = sparks.get(r["strategy_id"], [])[-SPARKLINE_DAYS:]
        return jsonify(rows), 200
    except Error as e:
        current_app.logger.error(f"Database error in get_scorecard: {e}")
        return jsonify({"error": str(e)}), 500


# GET /quant/strategies/<id>/history
# Daily strategy value alongside the strategy's benchmark close.
@quant_routes.route("/strategies/<int:strategy_id>/history", methods=["GET"])
def get_strategy_history(strategy_id):
    try:
        current_app.logger.info(f"GET /quant/strategies/{strategy_id}/history")
        rows = _run(
            """
            SELECT sh.record_date, sh.port_value, sh.daily_PNL, mp.adj_close AS benchmark_close
            FROM StrategyHistory sh
            LEFT JOIN (SELECT strat_bench, MIN(ticker) AS ticker
                       FROM Benchmark GROUP BY strat_bench) b
                   ON b.strat_bench = sh.strategy_id
            LEFT JOIN MarketPrice mp
                   ON mp.ticker = b.ticker AND mp.price_date = sh.record_date
            WHERE sh.strategy_id = %s
            ORDER BY sh.record_date
            """,
            (strategy_id,),
        )
        if not rows:
            return jsonify({"error": "No history for strategy"}), 404
        growth = _growth_index([r["port_value"] for r in rows], [r["daily_PNL"] for r in rows])
        for r, g in zip(rows, growth):
            r["record_date"] = r["record_date"].isoformat()
            r["growth_index"] = g
        return jsonify(rows), 200
    except Error as e:
        current_app.logger.error(f"Database error in get_strategy_history: {e}")
        return jsonify({"error": str(e)}), 500


# GET /quant/history?portfolio_ids=101,134
# Daily capital summed across the given portfolios' strategies (whole desk if
# omitted), with the S&P 500 close for reference.
@quant_routes.route("/history", methods=["GET"])
def get_capital_history():
    raw = request.args.get("portfolio_ids", "")
    ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    try:
        current_app.logger.info(f"GET /quant/history portfolio_ids={ids or 'all'}")
        where = f"WHERE s.port_strat IN ({','.join(['%s'] * len(ids))})" if ids else ""
        rows = _run(
            f"""
            SELECT sh.strategy_id, sh.record_date, sh.port_value, sh.daily_PNL
            FROM StrategyHistory sh
            JOIN Strategy s ON s.strategy_id = sh.strategy_id
            {where}
            ORDER BY sh.record_date
            """,
            tuple(ids),
        )
        spy = {r["price_date"]: float(r["adj_close"]) for r in
               _run("SELECT price_date, adj_close FROM MarketPrice WHERE ticker = 'SPY'")}
        # Aggregate by day: return = total P&L / total capital at the prior close
        by_day, last_value = {}, {}
        for r in rows:
            day = by_day.setdefault(r["record_date"], {"capital": 0.0, "pnl": 0.0, "prior": 0.0})
            if r["daily_PNL"] is not None and r["strategy_id"] in last_value:
                day["pnl"] += float(r["daily_PNL"])
                day["prior"] += last_value[r["strategy_id"]]
            last_value[r["strategy_id"]] = float(r["port_value"])
            day["capital"] += float(r["port_value"])
        out, level = [], 100.0
        for d in sorted(by_day):
            day = by_day[d]
            if day["prior"]:
                level *= 1 + day["pnl"] / day["prior"]
            out.append({"record_date": d.isoformat(), "strategy_capital": round(day["capital"], 2),
                        "daily_PNL": round(day["pnl"], 2), "growth_index": round(level, 6),
                        "spy_close": spy.get(d)})
        return jsonify(out), 200
    except Error as e:
        current_app.logger.error(f"Database error in get_capital_history: {e}")
        return jsonify({"error": str(e)}), 500


# GET /quant/portfolios/<id>/positions
# Holdings with ticker, weight inputs, and average analyst rating.
@quant_routes.route("/portfolios/<int:portfolio_id>/positions", methods=["GET"])
def get_positions(portfolio_id):
    try:
        current_app.logger.info(f"GET /quant/portfolios/{portfolio_id}/positions")
        rows = _run(
            """
            SELECT sp.position_id, a.asset_id, a.ticker, a.asset_name, a.asset_type,
                   sp.qty_held, sp.avg_cost, sp.market_value, sp.unrealized_PNL,
                   sp.price_target, ROUND(AVG(ar.rating), 1) AS avg_rating
            FROM StockPosition sp
            LEFT JOIN Asset a          ON a.pos_id = sp.position_id
            LEFT JOIN AnalystRating ar ON ar.asset_rating = a.asset_id
            WHERE sp.port_id = %s
            GROUP BY sp.position_id, a.asset_id, a.ticker, a.asset_name, a.asset_type,
                     sp.qty_held, sp.avg_cost, sp.market_value, sp.unrealized_PNL, sp.price_target
            ORDER BY sp.market_value DESC
            """,
            (portfolio_id,),
        )
        return jsonify(rows), 200
    except Error as e:
        current_app.logger.error(f"Database error in get_positions: {e}")
        return jsonify({"error": str(e)}), 500


# GET /quant/assets
# Lightweight asset list for pickers.
@quant_routes.route("/assets", methods=["GET"])
def get_assets():
    try:
        rows = _run("""SELECT a.asset_id, a.ticker, a.asset_name, mp.close_value AS last_close
                       FROM Asset a
                       LEFT JOIN MarketPrice mp ON mp.ticker = a.ticker
                            AND mp.price_date = (SELECT MAX(price_date) FROM MarketPrice WHERE ticker = a.ticker)
                       ORDER BY a.ticker, a.asset_id""")
        return jsonify(rows), 200
    except Error as e:
        current_app.logger.error(f"Database error in get_assets: {e}")
        return jsonify({"error": str(e)}), 500


# GET /quant/data-status
# Where the numbers come from: the latest refresh, price coverage, and T-bill rate.
@quant_routes.route("/data-status", methods=["GET"])
def get_data_status():
    try:
        latest = _run("SELECT * FROM DataRefresh ORDER BY refresh_id DESC LIMIT 1")
        last_ok = _run("SELECT * FROM DataRefresh WHERE status IN ('success', 'partial') "
                       "ORDER BY refresh_id DESC LIMIT 1")
        coverage = _run("""SELECT source, COUNT(DISTINCT ticker) AS tickers, COUNT(*) AS rows_count,
                                  MIN(price_date) AS first_date, MAX(price_date) AS last_date
                           FROM MarketPrice GROUP BY source""")
        rate = _run("SELECT rate_date, rate_pct FROM RiskFreeRate ORDER BY rate_date DESC LIMIT 1")
        live = any(c["source"] == "Yahoo" for c in coverage)
        body = {
            "mode": "live" if live else "demo",
            "latest_refresh": latest[0] if latest else None,
            "last_successful_refresh": last_ok[0] if last_ok else None,
            "coverage": coverage,
            "risk_free": rate[0] if rate else None,
        }
        for key in ("latest_refresh", "last_successful_refresh", "risk_free"):
            if body[key]:
                body[key] = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in body[key].items()}
        body["coverage"] = [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in c.items()}
                            for c in coverage]
        return jsonify(body), 200
    except Error as e:
        current_app.logger.error(f"Database error in get_data_status: {e}")
        return jsonify({"error": str(e)}), 500


# POST /quant/refresh
# Start a market data refresh in the background; poll /quant/data-status for progress.
@quant_routes.route("/refresh", methods=["POST"])
def post_refresh():
    started = start_background_refresh(current_app.config)
    if not started:
        return jsonify({"message": "A refresh is already running"}), 409
    return jsonify({"message": "Refresh started"}), 202


# GET /quant/strategies/<id>/backtest
# How the strategy was backtested: rules, parameters, costs, and results.
@quant_routes.route("/strategies/<int:strategy_id>/backtest", methods=["GET"])
def get_backtest(strategy_id):
    try:
        rows = _run("SELECT * FROM StrategyBacktest WHERE strategy_id = %s", (strategy_id,))
        if not rows:
            return jsonify({"error": "This strategy hasn't been backtested on market data yet"}), 404
        row = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in rows[0].items()}
        return jsonify(row), 200
    except Error as e:
        current_app.logger.error(f"Database error in get_backtest: {e}")
        return jsonify({"error": str(e)}), 500
