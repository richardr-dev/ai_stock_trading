-- J.A.R.V.I.S. Fund schema. Safe to re-run (idempotent).
CREATE SCHEMA IF NOT EXISTS market;

CREATE TABLE IF NOT EXISTS market.tickers (
    symbol        TEXT PRIMARY KEY,
    name          TEXT,
    market        TEXT NOT NULL,          -- US | ID
    sector        TEXT,
    industry      TEXT,
    currency      TEXT,
    fin_currency  TEXT,
    updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS market.prices (
    symbol     TEXT NOT NULL,
    date       DATE NOT NULL,
    open       DOUBLE PRECISION,
    high       DOUBLE PRECISION,
    low        DOUBLE PRECISION,
    close      DOUBLE PRECISION,
    adj_close  DOUBLE PRECISION,
    volume     DOUBLE PRECISION,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS prices_date_idx ON market.prices(date);

-- Point-in-time snapshot of fundamentals (one row per symbol per ingest day)
CREATE TABLE IF NOT EXISTS market.fundamentals (
    symbol            TEXT NOT NULL,
    as_of             DATE NOT NULL,
    price             DOUBLE PRECISION,
    market_cap        DOUBLE PRECISION,
    shares            DOUBLE PRECISION,
    beta              DOUBLE PRECISION,
    trailing_pe       DOUBLE PRECISION,
    forward_pe        DOUBLE PRECISION,
    trailing_eps      DOUBLE PRECISION,
    forward_eps       DOUBLE PRECISION,
    book_value        DOUBLE PRECISION,
    roe               DOUBLE PRECISION,
    gross_margin      DOUBLE PRECISION,
    operating_margin  DOUBLE PRECISION,
    profit_margin     DOUBLE PRECISION,
    revenue           DOUBLE PRECISION,
    revenue_growth    DOUBLE PRECISION,
    earnings_growth   DOUBLE PRECISION,
    total_debt        DOUBLE PRECISION,
    total_cash        DOUBLE PRECISION,
    debt_to_equity    DOUBLE PRECISION,
    free_cashflow     DOUBLE PRECISION,
    target_mean_price DOUBLE PRECISION,
    recommendation    DOUBLE PRECISION,
    raw               JSONB,
    PRIMARY KEY (symbol, as_of)
);

-- Annual cash-flow history used for the DCF base and growth estimate
CREATE TABLE IF NOT EXISTS market.cashflows (
    symbol         TEXT NOT NULL,
    period_end     DATE NOT NULL,
    operating_cf   DOUBLE PRECISION,
    capex          DOUBLE PRECISION,
    free_cashflow  DOUBLE PRECISION,
    PRIMARY KEY (symbol, period_end)
);

CREATE TABLE IF NOT EXISTS market.macro_rates (
    series_id  TEXT NOT NULL,
    date       DATE NOT NULL,
    value      DOUBLE PRECISION,
    source     TEXT,
    PRIMARY KEY (series_id, date)
);

CREATE TABLE IF NOT EXISTS market.dcf_valuations (
    symbol           TEXT NOT NULL,
    run_date         DATE NOT NULL,
    method           TEXT,               -- fcff_2stage | excess_return (banks) | n/a
    price            DOUBLE PRECISION,
    intrinsic_value  DOUBLE PRECISION,
    upside_pct       DOUBLE PRECISION,
    wacc_pct         DOUBLE PRECISION,
    growth_pct       DOUBLE PRECISION,
    terminal_g_pct   DOUBLE PRECISION,
    rf_pct           DOUBLE PRECISION,
    rate_sens_pct    DOUBLE PRECISION,   -- pct change in value for +100bp risk-free
    assumptions      JSONB,
    PRIMARY KEY (symbol, run_date)
);

CREATE TABLE IF NOT EXISTS market.signals (
    symbol          TEXT NOT NULL,
    run_date        DATE NOT NULL,
    market          TEXT,
    price           DOUBLE PRECISION,
    ret_1m          DOUBLE PRECISION,
    ret_3m          DOUBLE PRECISION,
    ret_6m          DOUBLE PRECISION,
    mom_12_1        DOUBLE PRECISION,
    pct_of_52w_high DOUBLE PRECISION,
    above_200d      BOOLEAN,
    vol_ann         DOUBLE PRECISION,
    atr_pct         DOUBLE PRECISION,
    dcf_upside      DOUBLE PRECISION,
    quality         DOUBLE PRECISION,
    revisions       DOUBLE PRECISION,
    composite       DOUBLE PRECISION,
    rank_in_market  INTEGER,
    factor_z        JSONB,
    PRIMARY KEY (symbol, run_date)
);

CREATE TABLE IF NOT EXISTS market.recommendations (
    run_date     DATE NOT NULL,
    symbol       TEXT NOT NULL,
    market       TEXT,
    action       TEXT,            -- BUY | HOLD | ADD | TRIM | SELL
    weight       DOUBLE PRECISION, -- of total portfolio
    amount_usd   DOUBLE PRECISION,
    price        DOUBLE PRECISION,
    stop_price   DOUBLE PRECISION,
    target_price DOUBLE PRECISION,
    reason       TEXT,
    PRIMARY KEY (run_date, symbol)
);

CREATE TABLE IF NOT EXISTS market.ai_briefs (
    id         SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT now(),
    run_date   DATE,
    model      TEXT,
    kind       TEXT,              -- brief | chat
    prompt     TEXT,
    content    TEXT
);


-- 3-month potential vs risk forecast (see jarvis/forecast.py)
CREATE TABLE IF NOT EXISTS market.forecast_calibration (
    decile INTEGER PRIMARY KEY, run_date DATE, n INTEGER, mean DOUBLE PRECISION, median DOUBLE PRECISION,
    p10 DOUBLE PRECISION, p90 DOUBLE PRECISION, prob_pos DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS market.forecasts (
    symbol TEXT NOT NULL, run_date DATE NOT NULL, market TEXT, price DOUBLE PRECISION, decile INTEGER,
    hist_median_3m DOUBLE PRECISION, hist_p10_3m DOUBLE PRECISION, hist_prob_pos DOUBLE PRECISION,
    analyst_3m DOUBLE PRECISION, exp_ret_3m DOUBLE PRECISION, low_3m DOUBLE PRECISION, high_3m DOUBLE PRECISION,
    prob_gain DOUBLE PRECISION, vol_3m DOUBLE PRECISION, var95_3m DOUBLE PRECISION, max_dd_1y DOUBLE PRECISION,
    beta DOUBLE PRECISION, reward_risk DOUBLE PRECISION, above_200d BOOLEAN, pareto BOOLEAN, rr_rank INTEGER,
    PRIMARY KEY (symbol, run_date)
);

-- Cross-market ranking (added later; ALTER keeps existing databases in sync)
ALTER TABLE market.signals ADD COLUMN IF NOT EXISTS global_score DOUBLE PRECISION;
ALTER TABLE market.signals ADD COLUMN IF NOT EXISTS global_rank INTEGER;

-- Convenience views (dropped + recreated so new columns show up)
DROP VIEW IF EXISTS market.v_latest_signals;
DROP VIEW IF EXISTS market.v_latest_portfolio;
DROP VIEW IF EXISTS market.v_latest_rates;
CREATE VIEW market.v_latest_signals AS
SELECT s.*, t.name, t.sector
FROM market.signals s
JOIN market.tickers t USING (symbol)
WHERE s.run_date = (SELECT max(run_date) FROM market.signals)
ORDER BY s.market, s.rank_in_market;

CREATE VIEW market.v_latest_portfolio AS
SELECT r.*, t.name, t.sector
FROM market.recommendations r
JOIN market.tickers t USING (symbol)
WHERE r.run_date = (SELECT max(run_date) FROM market.recommendations)
ORDER BY r.weight DESC;

CREATE VIEW market.v_latest_rates AS
SELECT DISTINCT ON (series_id) series_id, date, value, source
FROM market.macro_rates
ORDER BY series_id, date DESC;

-- Your real positions. shares = source of truth; value is re-computed from latest price.
CREATE TABLE IF NOT EXISTS market.holdings (
    symbol      TEXT PRIMARY KEY,
    shares      DOUBLE PRECISION NOT NULL,
    currency    TEXT,
    cost_basis  DOUBLE PRECISION,        -- optional, per share, local currency
    note        TEXT,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- Stocks you never want the model to buy (e.g. personal conviction, ethics, already-researched no's)
CREATE TABLE IF NOT EXISTS market.exclusions (
    symbol     TEXT PRIMARY KEY,
    reason     TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
