"""
Daily market data scheduler (runs as its own container; see docker-compose.yaml).

Every weekday at REFRESH_TIME (default 18:30 U.S. Eastern, after the close):
  1. Core refresh: strategy tickers, backtests, and dashboards (seconds)
  2. Universe refresh: every U.S.-listed stock and ETF (minutes)

On startup it catches up: if the last successful run is older than the latest
completed trading day, it runs immediately. Market holidays are harmless: the
run finds no new prices and records that.

Settings (api/.env):
  REFRESH_TIME=18:30        run time, U.S. Eastern
  REFRESH_UNIVERSE=true     set to false to refresh only the strategy tickers
"""

import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend.market.refresh import _connect, run_refresh
from backend.market.universe import run_universe_refresh

log = logging.getLogger("market.scheduler")
ET = ZoneInfo("America/New_York")


def _setting_time():
    hour, _, minute = os.getenv("REFRESH_TIME", "18:30").partition(":")
    return int(hour), int(minute or 0)


def latest_completed_trading_day(now):
    """Most recent weekday whose scheduled run time has passed (holidays aside)."""
    hour, minute = _setting_time()
    day = now.date()
    if now.weekday() >= 5 or (now.hour, now.minute) < (hour, minute):
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def next_run(now):
    hour, minute = _setting_time()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def last_success(conn, job):
    cur = conn.cursor(dictionary=True)
    # A 'partial' universe run stopped early (throttled), so it counts as not done and resumes
    cur.execute("SELECT MAX(finished_at) AS t FROM DataRefresh WHERE job = %s AND status = 'success'", (job,))
    row = cur.fetchone()
    cur.close()
    # Stored times are the container's local time (naive); astimezone() interprets them that way
    return row["t"].astimezone(ET) if row and row["t"] else None


def run_jobs(config, jobs):
    conn = _connect(config)
    try:
        if "core" in jobs:
            log.info(f"Core refresh: {run_refresh(conn)}")
        if "universe" in jobs:
            log.info(f"Universe refresh: {run_universe_refresh(conn)}")
    finally:
        conn.close()


def due_jobs(config, now, universe_enabled):
    """Jobs whose last success came before the latest completed trading day's run time."""
    hour, minute = _setting_time()
    day = latest_completed_trading_day(now)
    cutoff = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)
    conn = _connect(config)
    try:
        jobs = ["core"] + (["universe"] if universe_enabled else [])
        return [j for j in jobs if (last_success(conn, j) or datetime.min.replace(tzinfo=ET)) < cutoff]
    finally:
        conn.close()


def main():
    from backend.rest_entry import create_app
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config = create_app().config
    universe_enabled = os.getenv("REFRESH_UNIVERSE", "true").lower() != "false"

    for attempt in range(30):  # wait for MySQL to accept connections
        try:
            _connect(config).close()
            break
        except Exception as e:
            log.info(f"Waiting for the database ({e.__class__.__name__})")
            time.sleep(5)

    RETRY_MINUTES = 30
    while True:
        now = datetime.now(ET)
        jobs = due_jobs(config, now, universe_enabled)
        if jobs:
            log.info(f"Running {', '.join(jobs)} (catching up to {latest_completed_trading_day(now)})")
            run_jobs(config, jobs)
            still_due = due_jobs(config, datetime.now(ET), universe_enabled)
            if still_due:  # failed (e.g. source unreachable): retry later instead of looping
                log.info(f"{', '.join(still_due)} still due; retrying in {RETRY_MINUTES} minutes")
                time.sleep(RETRY_MINUTES * 60)
            continue
        wake = next_run(datetime.now(ET))
        log.info(f"Up to date. Next run {wake:%a %b %d %H:%M %Z}")
        while datetime.now(ET) < wake:
            time.sleep(min(300, max(1, (wake - datetime.now(ET)).total_seconds())))


if __name__ == "__main__":
    main()
