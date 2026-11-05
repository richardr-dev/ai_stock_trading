"""Data ingestion: prices + fundamentals (yfinance), rates (yfinance + FRED), into Postgres."""
from __future__ import annotations

import io
import logging
import subprocess
import time
from datetime import date

import pandas as pd
import yfinance as yf

from . import db
from .config import all_tickers, benchmarks, market_of, settings

log = logging.getLogger("jarvis.ingest")


# ── Prices ────────────────────────────────────────────────────────────────
def _symbols_with_history(min_rows: int = 500) -> set[str]:
    df = db.query("SELECT symbol FROM market.prices GROUP BY symbol HAVING count(*) >= :n", n=min_rows)
    return set(df["symbol"])


def ingest_prices(symbols: list[str], period: str | None = None) -> int:
    """New symbols get 6y of history; known symbols get a 6-month refresh (cheap, idempotent)."""
    if period:
        groups = {period: symbols}
    else:
        known = _symbols_with_history()
        groups = {"6mo": [s for s in symbols if s in known], "6y": [s for s in symbols if s not in known]}
    total = 0
    for per, syms in groups.items():
        if syms:
            log.info("Downloading prices for %d symbols (period=%s)", len(syms), per)
            total += _download(syms, per)
    return total


def _download(symbols: list[str], period: str) -> int:
    total = 0
    for i in range(0, len(symbols), 25):          # batches keep Yahoo happy
        batch = symbols[i:i + 25]
        data = yf.download(batch, period=period, auto_adjust=False, group_by="ticker",
                           threads=True, progress=False)
        if data is None or data.empty:
            continue
        for sym in batch:
            try:
                df = data[sym] if isinstance(data.columns, pd.MultiIndex) else data
            except KeyError:
                log.warning("No price data for %s", sym)
                continue
            df = df.dropna(subset=["Close"])
            if df.empty:
                continue
            rows = [{
                "symbol": sym, "date": idx.date(),
                "open": r["Open"], "high": r["High"], "low": r["Low"], "close": r["Close"],
                "adj_close": r.get("Adj Close", r["Close"]), "volume": r["Volume"],
            } for idx, r in df.iterrows()]
            total += db.upsert("market.prices", rows, ["symbol", "date"])
    return total


# ── Fundamentals ──────────────────────────────────────────────────────────
def _row_value(frame: pd.DataFrame, names: list[str], col) -> float | None:
    for n in names:
        if n in frame.index:
            v = frame.loc[n, col]
            if pd.notna(v):
                return float(v)
    return None


def ingest_fundamentals(symbols: list[str]) -> int:
    today = date.today()
    n = 0
    for sym in symbols:
        try:
            t = yf.Ticker(sym)
            info = t.info or {}
            if not info or info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
                log.warning("No info for %s", sym)
            db.upsert("market.tickers", [{
                "symbol": sym, "name": info.get("longName") or info.get("shortName"),
                "market": market_of(sym), "sector": info.get("sector"),
                "industry": info.get("industry"), "currency": info.get("currency"),
                "fin_currency": info.get("financialCurrency") or info.get("currency"),
                "updated_at": pd.Timestamp.utcnow().to_pydatetime(),
            }], ["symbol"])

            keep = {k: v for k, v in info.items()
                    if isinstance(v, (int, float, str, bool)) and k not in ("longBusinessSummary",)}
            db.upsert("market.fundamentals", [{
                "symbol": sym, "as_of": today,
                "price": info.get("currentPrice") or info.get("regularMarketPrice"),
                "market_cap": info.get("marketCap"), "shares": info.get("sharesOutstanding"),
                "beta": info.get("beta"), "trailing_pe": info.get("trailingPE"),
                "forward_pe": info.get("forwardPE"), "trailing_eps": info.get("trailingEps"),
                "forward_eps": info.get("forwardEps"), "book_value": info.get("bookValue"),
                "roe": info.get("returnOnEquity"), "gross_margin": info.get("grossMargins"),
                "operating_margin": info.get("operatingMargins"),
                "profit_margin": info.get("profitMargins"), "revenue": info.get("totalRevenue"),
                "revenue_growth": info.get("revenueGrowth"),
                "earnings_growth": info.get("earningsGrowth"),
                "total_debt": info.get("totalDebt"), "total_cash": info.get("totalCash"),
                "debt_to_equity": info.get("debtToEquity"),
                "free_cashflow": info.get("freeCashflow"),
                "target_mean_price": info.get("targetMeanPrice"),
                "recommendation": info.get("recommendationMean"),
                "raw": keep,
            }], ["symbol", "as_of"])

            cf = t.cashflow  # annual, columns = period end
            if cf is not None and not cf.empty:
                rows = []
                for col in cf.columns:
                    ocf = _row_value(cf, ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"], col)
                    capex = _row_value(cf, ["Capital Expenditure"], col)
                    fcf = _row_value(cf, ["Free Cash Flow"], col)
                    if fcf is None and ocf is not None and capex is not None:
                        fcf = ocf + capex
                    if fcf is None and ocf is None:
                        continue
                    rows.append({"symbol": sym, "period_end": pd.Timestamp(col).date(),
                                 "operating_cf": ocf, "capex": capex, "free_cashflow": fcf})
                db.upsert("market.cashflows", rows, ["symbol", "period_end"])
            n += 1
            time.sleep(0.4)
        except Exception as e:  # keep going on single-ticker failures
            log.warning("Fundamentals failed for %s: %s", sym, e)
    return n


# ── Rates ─────────────────────────────────────────────────────────────────
def _fred_series(series_id: str) -> pd.Series:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    # System curl: Python's getaddrinfo can hang on macOS for this host and isn't covered by timeouts.
    for attempt in range(3):
        p = subprocess.run(["curl", "-sfL", "--http1.1", "--max-time", "45", url],
                           capture_output=True, text=True)
        if p.returncode == 0 and p.stdout:
            break
        if attempt == 2:
            raise RuntimeError(f"curl exit {p.returncode}")
        time.sleep(3 * (attempt + 1))
    df = pd.read_csv(io.StringIO(p.stdout))
    df.columns = ["date", "value"]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna()
    return df[df["date"] >= pd.Timestamp.today() - pd.DateOffset(years=6)].set_index("date")["value"]


def ingest_rates() -> int:
    cfg = settings()["rates"]
    n = 0
    for sid in cfg.get("fred", []):
        try:
            s = _fred_series(sid)
            n += db.upsert("market.macro_rates",
                           [{"series_id": sid, "date": d.date(), "value": v, "source": "FRED"}
                            for d, v in s.items()], ["series_id", "date"])
        except Exception as e:
            log.warning("FRED %s failed: %s", sid, e)

    for name, yf_sym in cfg.get("yfinance", {}).items():
        try:
            h = yf.Ticker(yf_sym).history(period="6y", auto_adjust=False)
            if h.empty:
                continue
            n += db.upsert("market.macro_rates",
                           [{"series_id": name, "date": d.date(), "value": float(v), "source": "yfinance"}
                            for d, v in h["Close"].dropna().items()], ["series_id", "date"])
        except Exception as e:
            log.warning("yfinance rate %s failed: %s", yf_sym, e)

    # Manual policy-rate entries (BI rate etc.) stamped with today's date
    for k, v in cfg.get("manual", {}).items():
        n += db.upsert("market.macro_rates",
                       [{"series_id": k, "date": date.today(), "value": v, "source": "manual"}],
                       ["series_id", "date"])
    return n


def latest_rate(series_id: str, max_age_days: int | None = None) -> float | None:
    df = db.query("SELECT date, value FROM market.macro_rates WHERE series_id=:s "
                  "ORDER BY date DESC LIMIT 1", s=series_id)
    if df.empty:
        return None
    if max_age_days is not None and (pd.Timestamp.today() - pd.Timestamp(df["date"].iloc[0])).days > max_age_days:
        return None
    return float(df["value"].iloc[0])


def risk_free(market: str) -> float:
    """Risk-free rate in percent for a market (used as DCF discount base)."""
    if market == "US":
        return latest_rate("US_10Y", 10) or latest_rate("DGS10") or 4.2
    return (latest_rate("ID_10Y")
            or settings()["rates"]["manual"]["ID_10Y"])


def ingest_all(skip_fundamentals: bool = False) -> dict:
    db.init_db()
    tickers = all_tickers()
    symbols = [s for lst in tickers.values() for s in lst]
    out = {"rates": ingest_rates()}
    out["prices"] = ingest_prices(symbols + list(benchmarks().values()))
    if not skip_fundamentals:
        out["fundamentals"] = ingest_fundamentals(symbols)
    return out
