"""
Market data providers.

  * Yahoo Finance chart API: daily close and split/dividend-adjusted close,
    plus reference data (name, instrument type). No API key.
  * FRED series DTB3: 3-month U.S. Treasury bill rate (percent, annualized),
    the standard risk-free rate. No API key.

Both are free public endpoints meant for light use; the refresh job calls each
ticker once per run and stores the results in MySQL.
"""

import csv
import io
import json
import re
import time
import urllib.request
from datetime import date, datetime, timezone

import pandas as pd

USER_AGENT = "Mozilla/5.0 (PortIQ market data refresh)"
YAHOO_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
             "?period1={start}&period2={end}&interval=1d&events=split%2Cdiv&includeAdjustedClose=true")
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3&cosd={start}"

# Overflow guard only. Split-adjusted history for companies with repeated reverse splits
# can legitimately reach millions of dollars per share (e.g. VCIG above $100M), so this
# must sit far above real values; the price columns hold up to 10^16.
MAX_PRICE = 1e15
INSTRUMENT_TYPES = {"EQUITY": "Equity", "ETF": "ETF", "MUTUALFUND": "Fund", "INDEX": "Index"}


def _get(url, timeout=20, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.read()
        except Exception as e:  # network errors, HTTP 429/5xx
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url.split('?')[0]}: {last}")


def fetch_prices(ticker, start: date):
    """Daily prices since `start`. Returns (DataFrame[close, adj_close] indexed by date, reference dict)."""
    start_ts = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    url = YAHOO_URL.format(ticker=ticker, start=start_ts, end=int(time.time()))
    payload = json.loads(_get(url))
    result = (payload.get("chart") or {}).get("result")
    if not result:
        error = (payload.get("chart") or {}).get("error") or "no data"
        raise RuntimeError(f"Yahoo Finance returned no data for {ticker}: {error}")
    r = result[0]
    quote = r["indicators"]["quote"][0]
    adj = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose") or quote["close"]
    if not r.get("timestamp"):
        raise RuntimeError(f"Yahoo Finance returned no prices for {ticker}")
    frame = pd.DataFrame({
        "close": quote["close"],
        "adj_close": adj,
        "volume": quote.get("volume") or [None] * len(r["timestamp"]),
    }, index=pd.to_datetime(r["timestamp"], unit="s").normalize())
    frame = frame.dropna(subset=["close", "adj_close"])
    # Drop non-positive values and anything that would overflow the price columns
    frame = frame[(frame.close > 0) & (frame.adj_close > 0) & (frame.close < MAX_PRICE) & (frame.adj_close < MAX_PRICE)]
    frame = frame[~frame.index.duplicated(keep="last")]
    meta = r.get("meta", {})
    reference = {
        "name": (meta.get("longName") or meta.get("shortName") or ticker)[:50],
        "asset_type": INSTRUMENT_TYPES.get(meta.get("instrumentType"), meta.get("instrumentType")),
        "exchange": meta.get("fullExchangeName"),
        "currency": meta.get("currency"),
    }
    return frame, reference


SYMBOL_DIRECTORY = "https://www.nasdaqtrader.com/dynamic/SymDir/{file}.txt"
EXCLUDE_NAMES = re.compile(r"\b(warrants?|rights?|units?|preferred|notes? due|debentures?|subordinated|"
                           r"depositary shares?,? each representing)\b", re.I)
SHARE_CLASS = re.compile(r"^[A-Z]{1,5}\.[A-Z]{1,2}$")
SHARE_CLASS_YAHOO = re.compile(r"^[A-Z]{1,5}-[A-Z]{1,2}$")
EXCHANGES = {"N": "NYSE", "P": "NYSE Arca", "A": "NYSE American", "Z": "Cboe BZX", "V": "IEX"}


def fetch_symbol_directory():
    """Every U.S.-listed common stock and ETF from the Nasdaq Trader symbol directory.

    Excludes test listings, warrants, rights, units, preferreds, notes, and Nasdaq
    companies flagged as deficient or delinquent. Returns a list of dicts.
    """
    securities = {}
    nasdaq = _get(SYMBOL_DIRECTORY.format(file="nasdaqlisted")).decode()
    for r in csv.DictReader(io.StringIO(nasdaq), delimiter="|"):
        symbol = (r.get("Symbol") or "").strip()
        if (symbol.isalpha() and r.get("Test Issue") == "N" and r.get("Financial Status") in ("N", "")
                and not EXCLUDE_NAMES.search(r.get("Security Name", ""))):
            securities[symbol] = {"ticker": symbol, "name": r["Security Name"][:200], "exchange": "Nasdaq",
                                  "is_etf": r.get("ETF") == "Y"}
    other = _get(SYMBOL_DIRECTORY.format(file="otherlisted")).decode()
    for r in csv.DictReader(io.StringIO(other), delimiter="|"):
        # Share classes like BRK.B are written BRK-B on Yahoo Finance
        symbol = (r.get("ACT Symbol") or "").strip()
        if SHARE_CLASS.match(symbol):
            symbol = symbol.replace(".", "-")
        if ((symbol.isalpha() or SHARE_CLASS_YAHOO.match(symbol)) and r.get("Test Issue") == "N"
                and not EXCLUDE_NAMES.search(r.get("Security Name", ""))):
            securities.setdefault(symbol, {"ticker": symbol, "name": r["Security Name"][:200],
                                           "exchange": EXCHANGES.get(r.get("Exchange"), r.get("Exchange")),
                                           "is_etf": r.get("ETF") == "Y"})
    if len(securities) < 1000:
        raise RuntimeError(f"symbol directory looks incomplete ({len(securities)} securities)")
    return list(securities.values())


def fetch_risk_free(start: date):
    """3-month T-bill rate in percent, indexed by date (missing days dropped)."""
    raw = _get(FRED_URL.format(start=start.isoformat())).decode()
    frame = pd.read_csv(io.StringIO(raw))
    frame.columns = ["date", "rate_pct"]
    frame["rate_pct"] = pd.to_numeric(frame.rate_pct, errors="coerce")
    frame = frame.dropna()
    return pd.Series(frame.rate_pct.values, index=pd.DatetimeIndex(pd.to_datetime(frame.date)), name="rate_pct")
