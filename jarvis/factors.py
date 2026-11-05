"""Multi-factor stock scoring for a 2-3 month holding horizon.

Why these factors (all well documented in the academic / practitioner literature):
  * 12-1 momentum (Jegadeesh & Titman): winners of the past year, skipping the last month
    (short-term reversal), tend to keep winning for 3-12 months. This is the factor that would have
    had you overweight NVDA/MSFT while they were running.
  * 6m and risk-adjusted 3m momentum: faster confirmation; catches rotation earlier.
  * 52-week-high proximity (George & Hwang 2004): stocks near their highs drift higher.
  * Trend filter (price > 200d, 50d > 200d): avoids catching falling knives.
  * DCF value + quality: stops you from paying any price; momentum + value are negatively
    correlated, so mixing them smooths the ride.
  * Revisions (forward EPS vs trailing EPS, analyst target): earnings momentum.
  * Interest-rate regime: when the 10Y is rising fast, long-duration (rate-sensitive) names get
    penalised; when it is falling, they get a tailwind.

Scores are percentile-ranked *within each market* (US vs Indonesia separately) so the very
different volatility / valuation levels don't contaminate each other.
"""
from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd

from . import db
from .config import all_tickers, benchmarks, market_of, settings

log = logging.getLogger("jarvis.factors")

PRICE_FACTORS = ["mom_12_1", "mom_6m", "mom_3m_risk_adj", "high_52w", "trend", "low_vol"]


def rank_score(s: pd.Series) -> pd.Series:
    """Percentile rank mapped to ~unit-variance score in [-1.73, 1.73]; NaN -> 0 (neutral)."""
    r = s.rank(pct=True)
    return ((r - 0.5) * np.sqrt(12)).fillna(0.0)


def price_factors(px: pd.DataFrame) -> pd.DataFrame:
    """px: wide adj-close frame ending at the as-of date. Returns raw factor values per symbol."""
    px = px.ffill()
    if len(px) < 260:
        raise ValueError("need ~260 trading days of history")
    last = px.iloc[-1]
    rets = px.pct_change(fill_method=None)
    vol63 = rets.iloc[-63:].std() * np.sqrt(252)
    ma50, ma200 = px.iloc[-50:].mean(), px.iloc[-200:].mean()
    out = pd.DataFrame({
        "price": last,
        "ret_1m": last / px.iloc[-22] - 1,
        "ret_3m": last / px.iloc[-64] - 1,
        "ret_6m": last / px.iloc[-127] - 1,
        "mom_12_1": px.iloc[-22] / px.iloc[-253] - 1,
        "pct_of_52w_high": last / px.iloc[-252:].max(),
        "above_200d": last > ma200,
        "vol_ann": vol63,
    })
    out["mom_6m"] = out["ret_6m"]
    out["mom_3m_risk_adj"] = out["ret_3m"] / vol63.replace(0, np.nan)
    out["high_52w"] = out["pct_of_52w_high"]
    out["trend"] = (0.5 * (last / ma200 - 1).clip(-0.5, 0.5) + 0.5 * (ma50 / ma200 - 1).clip(-0.5, 0.5))
    out["low_vol"] = -vol63
    # require a valid price 1 year ago
    return out[px.iloc[-253].notna()]


def rate_regime(asof: date | None = None) -> dict:
    """Change in 10Y yield over ~3 months, per market. Positive = rates rising."""
    def chg(series_id: str, lookback_days: int = 90):
        df = db.query("SELECT date, value FROM market.macro_rates WHERE series_id=:s ORDER BY date", s=series_id)
        if df.empty:
            return None, None
        df["date"] = pd.to_datetime(df["date"])
        s = df.set_index("date")["value"]
        if asof:
            s = s[s.index <= pd.Timestamp(asof)]
        if len(s) < 2:
            return None, None
        past = s[s.index <= s.index[-1] - pd.Timedelta(days=lookback_days)]
        return float(s.iloc[-1]), (float(s.iloc[-1] - past.iloc[-1]) if len(past) else None)

    us_lvl, us_chg = chg("US_10Y")
    if us_lvl is None:
        us_lvl, us_chg = chg("DGS10")
    id_lvl, id_chg = chg("ID_10Y", 90)
    slope, _ = chg("T10Y2Y")

    def label(c):
        if c is None:
            return "unknown"
        return "rising" if c > 0.40 else "falling" if c < -0.40 else "stable"

    return {"US": {"10y": us_lvl, "chg_3m": us_chg, "regime": label(us_chg), "curve_10y2y": slope},
            "ID": {"10y": id_lvl, "chg_3m": id_chg, "regime": label(id_chg if id_chg is not None else us_chg),
                   "bi_rate": settings()["rates"]["manual"]["BI_RATE"]}}


def _atr_pct(symbol: str, n: int = 14) -> float | None:
    df = db.load_ohlc(symbol, start=pd.Timestamp.today() - pd.Timedelta(days=60))
    if len(df) < n + 1:
        return None
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return float(tr.iloc[-n:].mean() / df["close"].iloc[-1])


def run_signals(run_date: date | None = None) -> pd.DataFrame:
    run_date = run_date or date.today()
    w = settings()["factor_weights"]
    tickers = all_tickers()
    symbols = [s for lst in tickers.values() for s in lst]
    px = db.load_prices(symbols, start=pd.Timestamp(run_date) - pd.DateOffset(days=420))
    px = px[px.index <= pd.Timestamp(run_date)]
    pf = price_factors(px)

    fund = db.query("SELECT DISTINCT ON (symbol) * FROM market.fundamentals ORDER BY symbol, as_of DESC").set_index("symbol")
    dcf = db.query("SELECT DISTINCT ON (symbol) symbol, upside_pct, rate_sens_pct FROM market.dcf_valuations "
                   "ORDER BY symbol, run_date DESC").set_index("symbol")
    df = pf.join(fund[["roe", "gross_margin", "operating_margin", "debt_to_equity", "forward_eps",
                       "trailing_eps", "target_mean_price"]], how="left")
    df = df.join(dcf, how="left")
    df["market"] = [market_of(s) for s in df.index]

    # raw fundamental composites
    df["dcf_upside"] = df["upside_pct"].clip(-60, 100)
    eps_rev = (df["forward_eps"] / df["trailing_eps"] - 1).where(df["trailing_eps"] > 0).clip(-0.5, 1.0)
    tgt = (df["target_mean_price"] / df["price"] - 1).clip(-0.5, 1.0)
    regime = rate_regime(run_date)

    frames = []
    for mkt, g in df.groupby("market"):
        g = g.copy()
        z = pd.DataFrame(index=g.index)
        for f in PRICE_FACTORS:
            z[f] = rank_score(g[f])
        z["dcf_value"] = rank_score(g["dcf_upside"])
        q = pd.concat([rank_score(g["roe"].clip(-0.5, 0.6)), rank_score(g["gross_margin"]),
                       rank_score(g["operating_margin"]), rank_score(-g["debt_to_equity"])], axis=1)
        z["quality"] = q.mean(axis=1)
        z["revisions"] = pd.concat([rank_score(eps_rev.loc[g.index]), rank_score(tgt.loc[g.index])], axis=1).mean(axis=1)
        comp = sum(w[k] * z[k] for k in w)
        # rate-regime tilt on duration (rate_sens_pct is negative; more negative = longer duration)
        reg = regime[mkt]["regime"]
        dur = rank_score(g["rate_sens_pct"])  # high = less rate sensitive
        if reg == "rising":
            comp = comp + 0.08 * dur
        elif reg == "falling":
            comp = comp - 0.04 * dur
        z["rate_tilt"] = dur
        g["composite"] = comp
        g["quality_score"] = z["quality"]
        g["revisions_score"] = z["revisions"]
        g["rank_in_market"] = comp.rank(ascending=False, method="first").astype(int)
        g["factor_z"] = z.round(3).to_dict("records")
        frames.append(g)
    out = pd.concat(frames)

    # Global pass: same factors, ranked across BOTH markets together, so a crashing market's
    # "best" stock doesn't look as good as a rising market's best stock.
    gz = pd.DataFrame(index=out.index)
    for f_ in PRICE_FACTORS:
        gz[f_] = rank_score(out[f_])
    gz["dcf_value"] = rank_score(out["dcf_upside"])
    gz["quality"] = rank_score(out["quality_score"])
    gz["revisions"] = rank_score(out["revisions_score"])
    out["global_score"] = sum(w[k] * gz[k] for k in w)
    out["global_rank"] = out["global_score"].rank(ascending=False, method="first").astype(int)

    rows = []
    for sym, r in out.iterrows():
        rows.append({
            "symbol": sym, "run_date": run_date, "market": r["market"], "price": r["price"],
            "ret_1m": r["ret_1m"], "ret_3m": r["ret_3m"], "ret_6m": r["ret_6m"], "mom_12_1": r["mom_12_1"],
            "pct_of_52w_high": r["pct_of_52w_high"], "above_200d": bool(r["above_200d"]),
            "vol_ann": r["vol_ann"], "atr_pct": _atr_pct(sym), "dcf_upside": r["upside_pct"],
            "quality": r["quality_score"], "revisions": r["revisions_score"],
            "composite": r["composite"], "rank_in_market": int(r["rank_in_market"]),
            "global_score": r["global_score"], "global_rank": int(r["global_rank"]),
            "factor_z": r["factor_z"],
        })
    db.execute("DELETE FROM market.signals WHERE run_date = :d", d=run_date)
    db.upsert("market.signals", rows, ["symbol", "run_date"])
    return pd.DataFrame(rows).sort_values(["market", "rank_in_market"])


def benchmark_trend(run_date: date | None = None) -> dict:
    out = {}
    for mkt, sym in benchmarks().items():
        px = db.load_prices([sym])
        if px.empty:
            out[mkt] = None
            continue
        s = px[sym].dropna()
        if run_date:
            s = s[s.index <= pd.Timestamp(run_date)]
        out[mkt] = bool(s.iloc[-1] > s.iloc[-200:].mean()) if len(s) >= 200 else None
    return out
