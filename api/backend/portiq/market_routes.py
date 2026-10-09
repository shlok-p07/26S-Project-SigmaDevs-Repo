from datetime import date, datetime

import pandas as pd
from flask import Blueprint, jsonify, request, current_app
from mysql.connector import Error

from backend.db_connection import get_db
from backend.market import providers, strategies
from backend.market.refresh import upsert_prices
from backend.market.scheduler import next_run, ET

# Whole-market endpoints: universe status, market overview, security search and
# detail, what-if backtests, and parameter sweeps on any U.S. stock or ETF.
market_routes = Blueprint("market_routes", __name__)

LIQUID_DOLLAR_VOLUME = 20_000_000    # movers lists: at least $20M traded per day on average
BREADTH_DOLLAR_VOLUME = 1_000_000    # breadth: skip thinly traded names
SWEEPABLE = {"Momentum": "stop", "Mean Reversion": "zscore", "Rotation": "max", "Hedging": "hedge_band"}


def _run(query, params=()):
    cursor = get_db().cursor(dictionary=True)
    try:
        cursor.execute(query, params)
        return cursor.fetchall()
    finally:
        cursor.close()


def _clean(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "is_integer") or value.__class__.__name__ == "Decimal":
        return float(value)
    return value


def _clean_rows(rows):
    return [{k: _clean(v) for k, v in r.items()} for r in rows]


def _prices(ticker, fetch_if_missing=True):
    """Daily prices from MarketPrice; loads them from Yahoo Finance on first use."""
    rows = _run("SELECT price_date, close_value, adj_close, volume FROM MarketPrice "
                "WHERE ticker = %s AND source = 'Yahoo' ORDER BY price_date", (ticker,))
    if not rows and fetch_if_missing:
        frame, _ = providers.fetch_prices(ticker, date(2023, 10, 1))
        cur = get_db().cursor()
        upsert_prices(cur, ticker, frame)
        get_db().commit()
        cur.close()
        return frame
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame.index = pd.to_datetime(frame.pop("price_date"))
    frame.columns = ["close", "adj_close", "volume"]
    return frame.astype({"close": float, "adj_close": float})


def _risk_free(index):
    rows = _run("SELECT rate_date, rate_pct FROM RiskFreeRate ORDER BY rate_date")
    if not rows:
        return pd.Series(0.0, index=index)
    rates = pd.Series([float(r["rate_pct"]) for r in rows], index=pd.to_datetime([r["rate_date"] for r in rows]))
    return (rates.reindex(index).ffill().bfill() / 100 + 1) ** (1 / 252) - 1


# GET /quant/universe/status
@market_routes.route("/universe/status", methods=["GET"])
def universe_status():
    try:
        listed = _run("SELECT COUNT(*) AS n, SUM(is_etf) AS etfs FROM Security WHERE listed")[0]
        latest = _run("SELECT MAX(as_of) AS d FROM SecuritySnapshot")[0]["d"]
        current = _run("SELECT COUNT(*) AS n FROM SecuritySnapshot WHERE as_of = %s", (latest,))[0]["n"] if latest else 0
        job = _run("SELECT * FROM DataRefresh WHERE job = 'universe' ORDER BY refresh_id DESC LIMIT 1")
        return jsonify({
            "securities": int(listed["n"] or 0), "etfs": int(listed["etfs"] or 0),
            "current": int(current), "as_of": _clean(latest),
            "latest_job": _clean_rows(job)[0] if job else None,
            "next_scheduled": next_run(datetime.now(ET)).isoformat(),
        }), 200
    except Error as e:
        current_app.logger.error(f"Database error in universe_status: {e}")
        return jsonify({"error": str(e)}), 500


# GET /quant/market/overview
@market_routes.route("/market/overview", methods=["GET"])
def market_overview():
    try:
        latest = _run("SELECT MAX(as_of) AS d FROM SecuritySnapshot")[0]["d"]
        if not latest:
            return jsonify({"error": "The market universe hasn't been loaded yet"}), 404
        base = """FROM SecuritySnapshot ss JOIN Security s ON s.ticker = ss.ticker
                  WHERE ss.as_of = %s AND s.listed"""
        breadth = _run(f"""
            SELECT COUNT(*) AS stocks,
                   SUM(ss.change_pct > 0) AS advancers, SUM(ss.change_pct < 0) AS decliners,
                   SUM(ss.close_value > ss.ma50) AS above_50, SUM(ss.ma50 IS NOT NULL) AS with_50,
                   SUM(ss.close_value > ss.ma200) AS above_200, SUM(ss.ma200 IS NOT NULL) AS with_200,
                   SUM(ss.close_value >= ss.high_52w) AS new_highs, SUM(ss.close_value <= ss.low_52w) AS new_lows,
                   SUM(ss.dollar_volume) AS dollar_volume
            {base} AND NOT s.is_etf AND ss.avg_dollar_volume_20 >= %s""", (latest, BREADTH_DOLLAR_VOLUME))[0]
        columns = """ss.ticker, s.name, s.exchange, ss.close_value, ss.change_pct, ss.dollar_volume,
                     ss.ret_1m, ss.ret_1y"""
        liquid = f"{base} AND NOT s.is_etf AND ss.avg_dollar_volume_20 >= %s"
        movers = {
            "gainers": _run(f"SELECT {columns} {liquid} ORDER BY ss.change_pct DESC LIMIT 10",
                            (latest, LIQUID_DOLLAR_VOLUME)),
            "losers": _run(f"SELECT {columns} {liquid} ORDER BY ss.change_pct ASC LIMIT 10",
                           (latest, LIQUID_DOLLAR_VOLUME)),
            "most_active": _run(f"SELECT {columns} {base} ORDER BY ss.dollar_volume DESC LIMIT 10", (latest,)),
        }
        indexes = _run(f"""SELECT ss.ticker, s.name, ss.close_value, ss.change_pct, ss.ret_1m, ss.ret_3m, ss.ret_1y
                           {base} AND ss.ticker IN ('SPY', 'QQQ', 'IWM', 'DIA')""", (latest,))
        by_exchange = _run(f"""SELECT s.exchange, COUNT(*) AS n, AVG(ss.ret_1y) AS avg_ret_1y,
                                      SUM(ss.change_pct > 0) / COUNT(*) AS pct_up
                               {base} AND NOT s.is_etf AND ss.avg_dollar_volume_20 >= %s
                               GROUP BY s.exchange ORDER BY n DESC""", (latest, BREADTH_DOLLAR_VOLUME))
        returns = [float(r["ret_1y"]) for r in _run(
            f"SELECT ss.ret_1y {liquid} AND ss.ret_1y IS NOT NULL", (latest, BREADTH_DOLLAR_VOLUME))]
        return jsonify({
            "as_of": latest.isoformat(), "breadth": {k: _clean(v) for k, v in breadth.items()},
            "movers": {k: _clean_rows(v) for k, v in movers.items()},
            "indexes": _clean_rows(indexes), "by_exchange": _clean_rows(by_exchange), "returns_1y": returns,
        }), 200
    except Error as e:
        current_app.logger.error(f"Database error in market_overview: {e}")
        return jsonify({"error": str(e)}), 500


# GET /quant/securities/search?q=apple
@market_routes.route("/securities/search", methods=["GET"])
def search_securities():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([]), 200
    try:
        rows = _run("""
            SELECT s.ticker, s.name, s.exchange, s.is_etf, ss.close_value, ss.change_pct
            FROM Security s LEFT JOIN SecuritySnapshot ss ON ss.ticker = s.ticker
            WHERE s.listed AND (s.ticker = %s OR s.ticker LIKE %s OR s.name LIKE %s)
            ORDER BY s.ticker = %s DESC, s.ticker LIKE %s DESC, COALESCE(ss.avg_dollar_volume_20, 0) DESC
            LIMIT 25""", (q.upper(), q.upper() + "%", "%" + q + "%", q.upper(), q.upper() + "%"))
        return jsonify(_clean_rows(rows)), 200
    except Error as e:
        return jsonify({"error": str(e)}), 500


# GET /quant/securities/<ticker>
@market_routes.route("/securities/<ticker>", methods=["GET"])
def security_detail(ticker):
    ticker = ticker.upper()
    try:
        info = _run("SELECT * FROM Security WHERE ticker = %s", (ticker,))
        snap = _run("SELECT * FROM SecuritySnapshot WHERE ticker = %s", (ticker,))
        try:
            px = _prices(ticker)
        except Exception as e:
            return jsonify({"error": f"No prices available for {ticker}: {e}"}), 404
        spy = _prices("SPY", fetch_if_missing=False)
        spy_close = spy.adj_close if not spy.empty else pd.Series(dtype=float)
        prices = pd.DataFrame({"close": px.close, "adj_close": px.adj_close}).join(
            spy_close.rename("spy"), how="left")
        return jsonify({
            "security": _clean_rows(info)[0] if info else {"ticker": ticker, "name": ticker},
            "snapshot": _clean_rows(snap)[0] if snap else None,
            "prices": [{"date": d.date().isoformat(), "close": round(r.close, 4), "adj_close": round(r.adj_close, 4),
                        "spy": None if pd.isna(r.spy) else round(float(r.spy), 4)} for d, r in prices.iterrows()],
        }), 200
    except Error as e:
        return jsonify({"error": str(e)}), 500


def _what_if(ticker, strategy_type, params, start, capital):
    px = _prices(ticker)
    hedge_ticker = "SPY"
    hedge = _prices(hedge_ticker, fetch_if_missing=True)
    frame = pd.DataFrame({"asset": px.adj_close, "hedge": hedge.adj_close}).dropna()
    frame["rf"] = _risk_free(frame.index)
    return strategies.backtest(strategy_type, frame, params, start, capital)


def _summary(summary):
    return {k: (_clean(v.date()) if hasattr(v, "date") else v) for k, v in summary.items()}


# POST /quant/backtest  {ticker, strategy_type, parameter, start, capital}
# What-if backtest on any security; nothing is saved.
@market_routes.route("/backtest", methods=["POST"])
def what_if_backtest():
    body = request.get_json() or {}
    ticker = (body.get("ticker") or "").upper()
    strategy_type = body.get("strategy_type", "Passive")
    try:
        params, note = strategies.resolve_params(strategy_type, body.get("parameter", ""))
        start = date.fromisoformat(body.get("start") or "2024-01-02")
        daily, summary = _what_if(ticker, strategy_type, params, start, float(body.get("capital") or 100_000))
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    if daily is None:
        return jsonify({"error": summary}), 400
    px = _prices(ticker, fetch_if_missing=False).adj_close.reindex(daily.index)
    growth = (1 + daily.ret).cumprod() * 100
    return jsonify({
        "summary": _summary(summary), "params_used": params, "params_note": note,
        "daily": [{"date": d.date().isoformat(), "strategy": round(float(g), 4),
                   "buy_hold": round(float(p / px.iloc[0] * 100), 4)}
                  for d, g, p in zip(daily.index, growth, px)],
    }), 200


# POST /quant/backtest/sweep  {ticker, strategy_type, parameter, start, values: [...]}
# Runs the same backtest across settings of the type's key parameter.
@market_routes.route("/backtest/sweep", methods=["POST"])
def parameter_sweep():
    body = request.get_json() or {}
    ticker = (body.get("ticker") or "").upper()
    strategy_type = body.get("strategy_type")
    key = SWEEPABLE.get(strategy_type)
    if not key:
        return jsonify({"error": f"{strategy_type} has no parameter to sweep"}), 400
    try:
        base, _ = strategies.resolve_params(strategy_type, body.get("parameter", ""))
        start = date.fromisoformat(body.get("start") or "2024-01-02")
        results = []
        for value in body.get("values") or []:
            params = dict(base, **{key: float(value)})
            daily, summary = _what_if(ticker, strategy_type, params, start, 100_000)
            if daily is not None:
                results.append({"value": float(value), **{k: summary[k] for k in (
                    "total_return", "sharpe", "volatility", "drawdown", "trades", "buy_hold_return")}})
        return jsonify({"parameter": key, "results": results}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# POST /quant/assets  {ticker}
# Make any U.S. security tradable: creates its Asset row if it doesn't exist.
@market_routes.route("/assets", methods=["POST"])
def create_asset():
    ticker = ((request.get_json() or {}).get("ticker") or "").upper()
    try:
        existing = _run("SELECT asset_id FROM Asset WHERE ticker = %s ORDER BY asset_id LIMIT 1", (ticker,))
        if existing:
            return jsonify({"asset_id": existing[0]["asset_id"], "created": False}), 200
        info = _run("SELECT * FROM Security WHERE ticker = %s", (ticker,))
        if not info:
            return jsonify({"error": f"{ticker} isn't a listed U.S. security"}), 404
        s = info[0]
        cursor = get_db().cursor(dictionary=True)
        cursor.execute("SELECT COALESCE(MAX(asset_id), 0) + 1 AS n FROM Asset")
        asset_id = cursor.fetchone()["n"]
        cursor.execute("""INSERT INTO Asset (asset_id, asset_type, ticker, asset_name, exchange, pos_id)
                          VALUES (%s, %s, %s, %s, %s, NULL)""",
                       (asset_id, "ETF" if s["is_etf"] else "Equity", ticker, s["name"][:50], s["exchange"]))
        get_db().commit()
        cursor.close()
        return jsonify({"asset_id": asset_id, "created": True}), 201
    except Error as e:
        return jsonify({"error": str(e)}), 500
