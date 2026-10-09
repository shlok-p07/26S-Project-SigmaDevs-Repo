"""
Universe refresh: daily prices for every U.S.-listed stock and ETF.

  1. Refresh the security list from the Nasdaq Trader symbol directory
  2. Download prices for each security. The first load takes 3 years; later
     runs take the last 400 days (enough for 52-week and 200-day statistics,
     and it picks up split and dividend adjustments to recent prices)
  3. Save prices and a statistics snapshot (returns, moving averages, 52-week
     range, liquidity) for the Market Overview and Stock Lookup pages

Downloads run in parallel under a shared rate limit and back off when Yahoo
Finance throttles. Per-ticker progress is saved, so an interrupted run resumes,
and a run started after the last trading day's prices are in skips finished tickers.

Run from the API container:  python -m backend.market.universe
"""

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd

from backend.market import providers
from backend.market.refresh import _connect, upsert_prices

log = logging.getLogger("market.universe")

HISTORY_START = date(2023, 10, 1)   # first load: about 3 years
DAILY_LOOKBACK_DAYS = 400           # later runs: about 13 months
WORKERS = 6
MAX_REQUESTS_PER_SECOND = 6.0
ABORT_AFTER_THROTTLES = 40          # consecutive throttled requests before stopping for now
PROGRESS_EVERY = 250
MIN_COVERAGE = 0.95                 # share of listed securities that must be current for "success"


class RateLimiter:
    """Shared pacing across worker threads, with a global pause after throttling."""

    def __init__(self, per_second):
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_slot = time.monotonic()
        self.paused_until = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            slot = max(self.next_slot, now, self.paused_until)
            self.next_slot = slot + self.interval
        time.sleep(max(0.0, slot - time.monotonic()))

    def pause(self, seconds):
        with self.lock:
            self.paused_until = max(self.paused_until, time.monotonic() + seconds)


def snapshot(ticker, frame):
    """Latest statistics for one security from its daily prices."""
    px = frame.adj_close
    last = float(frame.close.iloc[-1])
    rets = px.pct_change().dropna()

    def ago(days):
        return float(px.iloc[-1] / px.iloc[-days - 1] - 1) if len(px) > days else None

    year = frame.tail(252)
    dollar = (frame.close * frame.volume.fillna(0)).tail(20)
    return (
        ticker, frame.index[-1].date(), round(last, 4),
        None if len(px) < 2 else round(float(px.iloc[-1] / px.iloc[-2] - 1), 4),
        None if pd.isna(frame.volume.iloc[-1]) else int(frame.volume.iloc[-1]),
        None if pd.isna(frame.volume.iloc[-1]) else round(last * float(frame.volume.iloc[-1]), 2),
        round(float(dollar.mean()), 2) if len(dollar) else None,
        round(float(frame.close.tail(50).mean()), 4) if len(frame) >= 50 else None,
        round(float(frame.close.tail(200).mean()), 4) if len(frame) >= 200 else None,
        round(float(year.close.max()), 4), round(float(year.close.min()), 4),
        None if ago(21) is None else round(ago(21), 4),
        None if ago(63) is None else round(ago(63), 4),
        None if ago(252) is None else round(ago(252), 4),
        round(float(rets.tail(252).std() * math.sqrt(252)), 4) if len(rets) > 20 else None,
    )


SNAPSHOT_SQL = """
    REPLACE INTO SecuritySnapshot (ticker, as_of, close_value, change_pct, volume, dollar_volume,
        avg_dollar_volume_20, ma50, ma200, high_52w, low_52w, ret_1m, ret_3m, ret_1y, vol_1y)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""


def run_universe_refresh(conn, limit=None):
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT GET_LOCK('portiq_universe_refresh', 0) AS got")
    if not cur.fetchone()["got"]:
        cur.close()
        return {"status": "skipped", "reason": "a universe refresh is already running"}
    cur.execute("INSERT INTO DataRefresh (started_at, job, status, source) "
                "VALUES (%s, 'universe', 'running', 'Nasdaq Trader + Yahoo Finance')", (datetime.now(),))
    refresh_id = cur.lastrowid
    conn.commit()
    today = date.today()
    notes = []

    def progress(done, ok, rows, message=None):
        cur.execute("UPDATE DataRefresh SET tickers_loaded = %s, price_rows = %s, message = %s "
                    "WHERE refresh_id = %s", (ok, rows, message or f"{done} processed", refresh_id))
        conn.commit()

    try:
        # ---- 1. Security list ------------------------------------------------
        securities = providers.fetch_symbol_directory()
        cur.executemany("""
            INSERT INTO Security (ticker, name, exchange, is_etf, listed, first_seen, last_seen)
            VALUES (%s, %s, %s, %s, TRUE, %s, %s)
            ON DUPLICATE KEY UPDATE name = VALUES(name), exchange = VALUES(exchange),
                is_etf = VALUES(is_etf), listed = TRUE, last_seen = VALUES(last_seen)""",
            [(s["ticker"], s["name"], s["exchange"], s["is_etf"], today, today) for s in securities])
        cur.execute("UPDATE Security SET listed = FALSE WHERE last_seen < %s", (today,))
        conn.commit()

        # Latest trading day, from SPY
        spy, _ = providers.fetch_prices("SPY", today - timedelta(days=10))
        latest = spy.index[-1].date()

        # ---- 2. Work list: skip tickers already current; retry failures last ----
        state = {r["ticker"]: r for r in _rows(cur, "SELECT * FROM SecurityFetchState")}
        tickers = [s["ticker"] for s in securities]
        todo = [t for t in tickers
                if not (state.get(t) and state[t]["status"] == "ok" and state[t]["last_price_date"]
                        and state[t]["last_price_date"] >= latest)
                and not (state.get(t) and state[t]["status"] == "no_data"
                         and state[t]["last_attempt"] and state[t]["last_attempt"].date() == today)]
        todo.sort(key=lambda t: (state.get(t, {}).get("fail_count", 0), t))
        if limit:
            todo = todo[:limit]
        log.info(f"Universe: {len(securities)} securities, {len(todo)} to update through {latest}")

        limiter = RateLimiter(MAX_REQUESTS_PER_SECOND)
        throttled = {"streak": 0}
        stop = threading.Event()

        def download(ticker):
            if stop.is_set():
                return ticker, None, "stopped"
            have = state.get(ticker, {}).get("last_price_date")
            start = max(HISTORY_START, today - timedelta(days=DAILY_LOOKBACK_DAYS)) if have else HISTORY_START
            for attempt in range(4):
                limiter.wait()
                try:
                    frame, _ = providers.fetch_prices(ticker, start)
                    throttled["streak"] = 0
                    return ticker, frame, None
                except Exception as e:
                    text = str(e)
                    if "429" in text or "Too Many Requests" in text:
                        throttled["streak"] += 1
                        limiter.pause(15 * (attempt + 1))
                        if throttled["streak"] >= ABORT_AFTER_THROTTLES:
                            stop.set()
                            return ticker, None, "throttled"
                        continue
                    return ticker, None, text[:250]
            return ticker, None, "throttled"

        done = ok = rows = failed = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [pool.submit(download, t) for t in todo]
            for future in as_completed(futures):
                ticker, frame, error = future.result()
                done += 1
                now = datetime.now()
                if frame is not None and len(frame):
                    try:
                        upsert_prices(cur, ticker, frame)
                        cur.execute(SNAPSHOT_SQL, snapshot(ticker, frame))
                        cur.execute("""REPLACE INTO SecurityFetchState
                                       (ticker, last_price_date, last_attempt, status, fail_count, message)
                                       VALUES (%s, %s, %s, 'ok', 0, NULL)""", (ticker, frame.index[-1].date(), now))
                        ok += 1
                        rows += len(frame)
                    except Exception as e:  # bad data for one ticker: record it and keep going
                        conn.rollback()
                        frame, error = None, f"could not store prices: {e}"[:250]
                        log.warning(f"{ticker}: {error}")
                if frame is None and error != "stopped":
                    status = "no_data" if error and "no " in error.lower() else "failed"
                    prev = state.get(ticker, {})
                    cur.execute("""REPLACE INTO SecurityFetchState
                                   (ticker, last_price_date, last_attempt, status, fail_count, message)
                                   VALUES (%s, %s, %s, %s, %s, %s)""",
                                (ticker, prev.get("last_price_date"), now, status,
                                 prev.get("fail_count", 0) + 1, error))
                    failed += 1
                if done % 50 == 0:
                    conn.commit()
                if done % PROGRESS_EVERY == 0:
                    progress(done, ok, rows, f"{done} of {len(todo)} processed")
                    log.info(f"Universe: {done}/{len(todo)} processed, {ok} updated")
        conn.commit()

        current = _rows(cur, "SELECT COUNT(*) AS n FROM SecurityFetchState WHERE status = 'ok' "
                             "AND last_price_date >= %s", (latest,))[0]["n"]
        if stop.is_set():
            notes.append("Yahoo Finance throttled requests; stopped early. The next run resumes where this stopped")
        if failed:
            notes.append(f"{failed} securities had no data or failed (often newly listed or recently delisted)")
        # Success means the market is actually covered, not just that this run ended cleanly
        # (a trial run with a limit, or one stopped by throttling, is partial and resumes later)
        coverage = current / max(len(securities), 1)
        status = "success" if coverage >= MIN_COVERAGE and not stop.is_set() else "partial"
        notes.insert(0, f"{current:,} of {len(securities):,} securities current through {latest}")
        cur.execute("""UPDATE DataRefresh SET finished_at = %s, status = %s, as_of_date = %s, tickers_loaded = %s,
                              price_rows = %s, message = %s WHERE refresh_id = %s""",
                    (datetime.now(), status, latest, ok, rows, "\n".join(notes), refresh_id))
        conn.commit()
        log.info(f"Universe refresh {refresh_id} {status}: {notes[0]}")
        return {"refresh_id": refresh_id, "status": status, "updated": ok, "failed": failed, "current": current}
    except Exception as e:
        conn.rollback()
        cur.execute("UPDATE DataRefresh SET finished_at = %s, status = 'failed', message = %s WHERE refresh_id = %s",
                    (datetime.now(), str(e)[:1000], refresh_id))
        conn.commit()
        log.exception("Universe refresh failed")
        return {"refresh_id": refresh_id, "status": "failed", "error": str(e)}
    finally:
        cur.execute("DO RELEASE_LOCK('portiq_universe_refresh')")
        cur.close()


def _rows(cur, query, params=()):
    cur.execute(query, params)
    return cur.fetchall()


if __name__ == "__main__":
    import sys
    from backend.rest_entry import create_app
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = create_app()
    conn = _connect(app.config)
    try:
        print(run_universe_refresh(conn, limit=int(sys.argv[1]) if len(sys.argv) > 1 else None))
    finally:
        conn.close()
