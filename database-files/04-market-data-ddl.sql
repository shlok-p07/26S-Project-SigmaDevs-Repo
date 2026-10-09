-- Market data and backtest tables for the Quant Trader workspace.
-- Filled by the market data refresh (api/backend/market/refresh.py) with real
-- prices from Yahoo Finance and T-bill rates from FRED. Until the first refresh,
-- 05-demo-history.sql provides calibrated demo history so the app works offline.
USE PortIQ;

-- Daily prices for every ticker the app uses (holdings and benchmarks)
CREATE TABLE IF NOT EXISTS MarketPrice (
    ticker VARCHAR(20) NOT NULL,
    price_date DATE NOT NULL,
    close_value DECIMAL(20,4) NOT NULL,
    adj_close DECIMAL(20,4) NOT NULL,          -- adjusted for splits and dividends
    volume BIGINT,
    source VARCHAR(20) NOT NULL DEFAULT 'demo',
    PRIMARY KEY (ticker, price_date),
    KEY idx_price_date (price_date)
);

-- 3-month U.S. Treasury bill rate (FRED series DTB3), the risk-free rate
CREATE TABLE IF NOT EXISTS RiskFreeRate (
    rate_date DATE PRIMARY KEY,
    rate_pct DECIMAL(6,3) NOT NULL,
    source VARCHAR(20) NOT NULL DEFAULT 'FRED'
);

-- Daily equity for each strategy (backtested on MarketPrice, or demo)
CREATE TABLE IF NOT EXISTS StrategyHistory (
    history_id INT PRIMARY KEY AUTO_INCREMENT,
    strategy_id INT NOT NULL,
    record_date DATE NOT NULL,
    port_value DECIMAL(15,2) NOT NULL,
    daily_PNL DECIMAL(15,2),
    UNIQUE KEY uq_strategy_day (strategy_id, record_date),
    CONSTRAINT strat_history FOREIGN KEY (strategy_id)
        REFERENCES Strategy(strategy_id) ON DELETE CASCADE
);

-- How each strategy was backtested: rules, parameters, costs, and results
CREATE TABLE IF NOT EXISTS StrategyBacktest (
    strategy_id INT PRIMARY KEY,
    engine VARCHAR(30) NOT NULL,
    ticker VARCHAR(20) NOT NULL,
    hedge_ticker VARCHAR(20),
    params_used VARCHAR(120) NOT NULL,
    params_note VARCHAR(200),                   -- set when stored parameters don't fit the type
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    trading_days INT NOT NULL,
    initial_capital DECIMAL(15,2) NOT NULL,
    contributions DECIMAL(15,2) NOT NULL DEFAULT 0,
    trades_count INT NOT NULL,
    turnover DECIMAL(10,2) NOT NULL,
    cost_bps DECIMAL(6,2) NOT NULL,
    total_return DECIMAL(10,4) NOT NULL,
    cagr DECIMAL(10,4),
    buy_hold_return DECIMAL(10,4),
    metrics_window_days INT NOT NULL,
    run_at DATETIME NOT NULL,
    CONSTRAINT strat_backtest FOREIGN KEY (strategy_id)
        REFERENCES Strategy(strategy_id) ON DELETE CASCADE
);

-- Audit log of every market data refresh
CREATE TABLE IF NOT EXISTS DataRefresh (
    refresh_id INT PRIMARY KEY AUTO_INCREMENT,
    started_at DATETIME NOT NULL,
    finished_at DATETIME,
    job VARCHAR(20) NOT NULL DEFAULT 'core',    -- core (strategy tickers + backtests) or universe
    status VARCHAR(20) NOT NULL,                -- running, success, partial, failed
    source VARCHAR(60) NOT NULL,
    as_of_date DATE,
    tickers_loaded INT DEFAULT 0,
    price_rows INT DEFAULT 0,
    strategies_backtested INT DEFAULT 0,
    message TEXT
);

-- Every U.S.-listed stock and ETF (Nasdaq Trader symbol directory)
CREATE TABLE IF NOT EXISTS Security (
    ticker VARCHAR(20) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    exchange VARCHAR(30) NOT NULL,
    is_etf BOOLEAN NOT NULL,
    listed BOOLEAN NOT NULL DEFAULT TRUE,      -- false once it disappears from the directory
    first_seen DATE NOT NULL,
    last_seen DATE NOT NULL,
    KEY idx_security_name (name)
);

-- Latest statistics per security, recomputed on each universe refresh
CREATE TABLE IF NOT EXISTS SecuritySnapshot (
    ticker VARCHAR(20) PRIMARY KEY,
    as_of DATE NOT NULL,
    close_value DECIMAL(20,4) NOT NULL,
    change_pct DECIMAL(10,4),
    volume BIGINT,
    dollar_volume DECIMAL(20,2),
    avg_dollar_volume_20 DECIMAL(20,2),
    ma50 DECIMAL(20,4),
    ma200 DECIMAL(20,4),
    high_52w DECIMAL(20,4),
    low_52w DECIMAL(20,4),
    ret_1m DECIMAL(10,4),
    ret_3m DECIMAL(10,4),
    ret_1y DECIMAL(10,4),
    vol_1y DECIMAL(10,4),
    KEY idx_snapshot_as_of (as_of)
);

-- Per-ticker progress, so an interrupted universe load resumes where it stopped
CREATE TABLE IF NOT EXISTS SecurityFetchState (
    ticker VARCHAR(20) PRIMARY KEY,
    last_price_date DATE,
    last_attempt DATETIME,
    status VARCHAR(20) NOT NULL,               -- ok, failed, no_data
    fail_count INT NOT NULL DEFAULT 0,
    message VARCHAR(255)
);
