-- ============================================================================
-- PortIQ: Quant Trader KPI and Business Analysis Queries
-- Author: Shlok Patel
--
-- Business questions a trading desk lead would ask, answered against the
-- PortIQ schema (database-files/01-main-ddl.sql). Each query lists the
-- question it answers and the decision it supports.
--
-- Run:  docker exec -i mysql_db mysql -uroot -p"$MYSQL_ROOT_PASSWORD" --table PortIQ < analytics/quant_trader_kpis.sql
-- ============================================================================

USE PortIQ;


-- ----------------------------------------------------------------------------
-- Q1. Strategy scorecard
-- Question: Which strategies earn the best risk-adjusted return, and which
--           ones break our risk limits?
-- Decision: Where to allocate more capital; which strategies to review or pause.
-- Business rules (same as the Risk Analysis screen and quant_ui.verdict):
--   Sharpe >= 1.0 healthy | volatility <= 15% healthy | drawdown <= 10% healthy
-- ----------------------------------------------------------------------------
SELECT
    s.strategy_id,
    s.strategy_name,
    s.strategy_type,
    s.status,
    ROUND(r.sharpe_ratio, 2)                                         AS sharpe,
    ROUND(r.volatility * 100, 1)                                     AS volatility_pct,
    ROUND(r.drawdown * 100, 1)                                       AS drawdown_pct,
    ROUND(p.cumulative_PNL / NULLIF(p.port_value - p.cumulative_PNL, 0) * 100, 1)
                                                                     AS return_on_capital_pct,
    RANK() OVER (ORDER BY r.sharpe_ratio DESC)                       AS sharpe_rank,
    CASE
        WHEN r.sharpe_ratio >= 1.0 AND r.vol <= 0.15 AND r.dd <= 0.10 THEN 'Healthy'
        WHEN r.dd > 0.10                                              THEN 'Review: drawdown limit breached'
        WHEN r.sharpe_ratio < 1.0                                     THEN 'Review: weak risk-adjusted return'
        ELSE 'Watch: elevated volatility'
    END                                                              AS risk_verdict
FROM Strategy s
-- Risk metrics are FLOAT columns: 0.10 is stored as 0.100000001, so compare
-- rounded values or strategies sitting exactly on a limit count as breaches.
JOIN (SELECT *, ROUND(volatility, 4) AS vol, ROUND(drawdown, 4) AS dd
      FROM RiskMetric) r  ON r.risk_strat = s.strategy_id
JOIN PerformanceRecord p  ON p.strat_perf = s.strategy_id
ORDER BY sharpe_rank
LIMIT 15;


-- ----------------------------------------------------------------------------
-- Q2. Performance by strategy type
-- Question: Which trading approaches (momentum, mean reversion, hedging...)
--           work best across the whole book?
-- Decision: Which strategy types get more research and development time.
-- ----------------------------------------------------------------------------
SELECT
    s.strategy_type,
    COUNT(*)                                         AS strategies,
    SUM(s.status = 'active')                         AS active,
    ROUND(AVG(r.sharpe_ratio), 2)                    AS avg_sharpe,
    ROUND(AVG(r.drawdown) * 100, 1)                  AS avg_drawdown_pct,
    ROUND(SUM(p.cumulative_PNL), 0)                  AS total_cumulative_pnl,
    ROUND(SUM(p.cumulative_PNL) / SUM(SUM(p.cumulative_PNL)) OVER () * 100, 1)
                                                     AS share_of_total_pnl_pct
FROM Strategy s
JOIN RiskMetric r         ON r.risk_strat = s.strategy_id
JOIN PerformanceRecord p  ON p.strat_perf = s.strategy_id
GROUP BY s.strategy_type
ORDER BY avg_sharpe DESC;


-- ----------------------------------------------------------------------------
-- Q3. Benchmark coverage by index
-- Question: Which indexes are our strategies measured against, and how do
--           strategies grouped by benchmark compare on risk-adjusted return?
-- Decision: Whether each strategy is being judged against the right yardstick.
-- ----------------------------------------------------------------------------
SELECT
    b.ticker                                         AS benchmark,
    b.benchmark_name,
    COUNT(DISTINCT s.strategy_id)                    AS strategies_benchmarked,
    ROUND(AVG(r.sharpe_ratio), 2)                    AS avg_sharpe,
    SUM(r.sharpe_ratio >= 1.0)                       AS strategies_meeting_sharpe_target
FROM Benchmark b
JOIN Strategy s    ON s.strategy_id = b.strat_bench
JOIN RiskMetric r  ON r.risk_strat  = s.strategy_id
GROUP BY b.ticker, b.benchmark_name
ORDER BY strategies_benchmarked DESC;


-- ----------------------------------------------------------------------------
-- Q4. Portfolio concentration risk (Herfindahl-Hirschman Index)
-- Question: Which portfolios depend too much on a few positions?
-- Decision: Flag portfolios for rebalancing. HHI uses the 0-10,000 scale;
--           above 2,500 counts as highly concentrated (the U.S. antitrust convention).
-- ----------------------------------------------------------------------------
WITH position_weights AS (
    SELECT
        sp.port_id,
        sp.position_id,
        sp.market_value / SUM(sp.market_value) OVER (PARTITION BY sp.port_id) AS weight
    FROM StockPosition sp
)
SELECT
    pf.portfolio_id,
    pf.portfolio_name,
    COUNT(*)                                         AS positions,
    ROUND(MAX(w.weight) * 100, 1)                    AS largest_position_pct,
    ROUND(SUM(POW(w.weight * 100, 2)), 0)            AS hhi,
    CASE
        WHEN SUM(POW(w.weight * 100, 2)) > 2500 THEN 'Highly concentrated'
        WHEN SUM(POW(w.weight * 100, 2)) > 1500 THEN 'Moderately concentrated'
        ELSE 'Diversified'
    END                                              AS concentration
FROM position_weights w
JOIN Portfolio pf ON pf.portfolio_id = w.port_id
GROUP BY pf.portfolio_id, pf.portfolio_name
HAVING COUNT(*) > 1   -- a single-position portfolio is trivially 100% concentrated
ORDER BY hhi DESC
LIMIT 10;


-- ----------------------------------------------------------------------------
-- Q5. Trading activity by ticker
-- Question: Where is trading volume concentrated, and are we net buyers or
--           net sellers of each name?
-- Decision: Spot crowded trades and check activity against strategy intent.
-- ----------------------------------------------------------------------------
SELECT
    a.ticker,
    COUNT(*)                                                         AS trades,
    ROUND(SUM(t.quantity * t.price), 0)                              AS gross_notional,
    ROUND(SUM(CASE WHEN t.trade_type = 'BUY'  THEN t.quantity * t.price
                   WHEN t.trade_type = 'SELL' THEN -t.quantity * t.price END), 0)
                                                                     AS net_notional,
    ROUND(SUM(t.quantity * t.price) / SUM(SUM(t.quantity * t.price)) OVER () * 100, 1)
                                                                     AS share_of_volume_pct
FROM Trade t
JOIN Asset a ON a.asset_id = t.trade_asset
GROUP BY a.ticker
ORDER BY gross_notional DESC
LIMIT 10;


-- ----------------------------------------------------------------------------
-- Q6. Analyst consensus vs. position outcome
-- Question: Do the assets analysts rate highly actually make us money?
-- Decision: How much weight to give analyst ratings in trading decisions.
-- ----------------------------------------------------------------------------
SELECT
    ar.rating                                        AS analyst_rating,
    COUNT(DISTINCT a.asset_id)                       AS assets,
    ROUND(AVG(sp.unrealized_PNL / NULLIF(sp.avg_cost * sp.qty_held, 0)) * 100, 1)
                                                     AS avg_unrealized_return_pct,
    ROUND(AVG((sp.price_target - sp.avg_cost) / NULLIF(sp.avg_cost, 0)) * 100, 1)
                                                     AS avg_upside_to_target_pct
FROM AnalystRating ar
JOIN Asset a          ON a.asset_id     = ar.asset_rating
JOIN StockPosition sp ON sp.position_id = a.pos_id
GROUP BY ar.rating
ORDER BY ar.rating DESC;


-- ----------------------------------------------------------------------------
-- Q7. Data quality checks
-- Question: Can we trust the numbers above?
-- Decision: Stop bad data from reaching the dashboards. Every count should be 0.
-- ----------------------------------------------------------------------------
SELECT 'Strategies with no risk metric' AS check_name,
       COUNT(*) AS failures
FROM Strategy s LEFT JOIN RiskMetric r ON r.risk_strat = s.strategy_id
WHERE r.riskmetric_id IS NULL
UNION ALL
SELECT 'Strategies with no performance record',
       COUNT(*)
FROM Strategy s LEFT JOIN PerformanceRecord p ON p.strat_perf = s.strategy_id
WHERE p.performance_id IS NULL
UNION ALL
SELECT 'Strategies with no benchmark',
       COUNT(*)
FROM Strategy s LEFT JOIN Benchmark b ON b.strat_bench = s.strategy_id
WHERE b.benchmark_id IS NULL
UNION ALL
SELECT 'Trades with non-positive price or quantity',
       COUNT(*)
FROM Trade WHERE price <= 0 OR quantity <= 0
UNION ALL
SELECT 'Trades with unknown type (not BUY/SELL)',
       COUNT(*)
FROM Trade WHERE trade_type NOT IN ('BUY', 'SELL')
UNION ALL
SELECT 'Risk metrics outside plausible range',
       COUNT(*)
FROM RiskMetric
WHERE volatility NOT BETWEEN 0 AND 2 OR drawdown NOT BETWEEN 0 AND 1
UNION ALL
SELECT 'Tickers with conflicting asset types',
       COUNT(*)
FROM (SELECT ticker FROM Asset GROUP BY ticker HAVING COUNT(DISTINCT asset_type) > 1) conflicts;

-- Drill-down for the ticker check above
SELECT ticker,
       GROUP_CONCAT(DISTINCT asset_type ORDER BY asset_type) AS asset_types_found,
       COUNT(*)                                              AS asset_rows
FROM Asset
GROUP BY ticker
HAVING COUNT(DISTINCT asset_type) > 1
ORDER BY ticker;
