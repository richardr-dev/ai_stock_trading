"""3-month potential vs risk forecast.

How the forecast is built (transparent, data-driven, no magic):
  1. CALIBRATION — every month-end in the last ~5 years, score every stock with the price factors
     (momentum, 52w-high, trend, low-vol), bucket the scores into deciles, and record each stock's
     ACTUAL next-63-trading-day return. Result: "stocks in score decile d have historically returned
     X% over the next 3 months, with a 10th-percentile outcome of Y% and Z% chance of being positive".
  2. FORECAST — today's decile → historical median 3m return, blended with the analysts' 12-month
     target (scaled to 3 months, capped). DCF is shown but NOT used here (too noisy for 3 months).
  3. RISK — from each stock's own history: 3-month volatility, historical 5th-percentile 3-month
     return (VaR95), 1-year max drawdown, beta vs its benchmark.
  4. RANK — reward/risk = expected 3m return / 3m volatility; Pareto flag = no other stock has both
     higher expected return AND lower risk.

Caveats: survivorship bias (today's universe), regimes change, analyst targets lag. Use it to rank
and size, not as a promise. A 3-month "forecast" for a single stock is mostly noise: look at the range.
"""
from __future__ import annotations

import logging
from datetime import date
from math import erf, sqrt

import numpy as np
import pandas as pd

from . import db
from .config import all_tickers, benchmarks, market_of, settings
from .factors import PRICE_FACTORS, price_factors, rank_score

log = logging.getLogger("jarvis.forecast")
H = 63  # ~3 months of trading days


def _price_score(pf: pd.DataFrame) -> pd.Series:
    w = {k: v for k, v in settings()["factor_weights"].items() if k in PRICE_FACTORS}
    tot = sum(w.values())
    return sum((v / tot) * rank_score(pf[k]) for k, v in w.items())


def calibrate(px: pd.DataFrame) -> pd.DataFrame:
    """Historical forward-3m return stats by price-score decile (pooled US + ID)."""
    month_ends = px.groupby(px.index.to_period("M")).tail(1).index
    rows = []
    for d in month_ends:
        i = px.index.get_loc(d)
        if i < 260 or i + H >= len(px):
            continue
        hist = px.iloc[: i + 1]
        try:
            pf = price_factors(hist).dropna(subset=["mom_12_1"])
        except ValueError:
            continue
        sc = _price_score(pf)
        fwd = (px.iloc[i + H] / px.iloc[i] - 1).reindex(sc.index)
        dec = pd.qcut(sc.rank(method="first"), 10, labels=False) + 1
        rows.append(pd.DataFrame({"decile": dec, "fwd": fwd}).dropna())
    allr = pd.concat(rows)
    cal = allr.groupby("decile")["fwd"].agg(
        n="count", mean="mean", median="median",
        p10=lambda s: s.quantile(0.10), p90=lambda s: s.quantile(0.90),
        prob_pos=lambda s: (s > 0).mean())
    return cal.reset_index()


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + erf(x / sqrt(2)))


def run_forecast(run_date: date | None = None) -> pd.DataFrame:
    run_date = run_date or date.today()
    syms = [s for lst in all_tickers().values() for s in lst]
    bmk = benchmarks()
    px_all = db.load_prices(syms + list(bmk.values()))
    # per-symbol forward fill only inside its own history (markets have different holidays)
    px_all = px_all.ffill(limit=5)
    px = px_all[[s for s in syms if s in px_all]]

    cal = calibrate(px)
    db.execute("DELETE FROM market.forecast_calibration")
    db.upsert("market.forecast_calibration", cal.assign(run_date=run_date), ["decile"])

    pf = price_factors(px).dropna(subset=["mom_12_1"])
    sc = _price_score(pf)
    dec_today = pd.qcut(sc.rank(method="first"), 10, labels=False) + 1
    calm = cal.set_index("decile")

    fund = db.query("""SELECT DISTINCT ON (symbol) symbol, target_mean_price, market_cap
                       FROM market.fundamentals ORDER BY symbol, as_of DESC""").set_index("symbol")
    rets = px.pct_change(fill_method=None)
    brets = px_all[list(bmk.values())].pct_change(fill_method=None)
    rows = []
    for s in sc.index:
        mkt = market_of(s)
        c = px[s].dropna()
        r = rets[s].dropna()
        last = float(c.iloc[-1])
        vol_ann = float(r.iloc[-252:].std() * np.sqrt(252))
        vol_3m = vol_ann * np.sqrt(H / 252)
        roll = (c / c.shift(H) - 1).dropna().iloc[-756:]                  # 3y of rolling 3m returns
        var95 = float(roll.quantile(0.05)) if len(roll) > 100 else -1.65 * vol_3m
        dd = float((c.iloc[-252:] / c.iloc[-252:].cummax() - 1).min())
        b = brets[bmk[mkt]].reindex(r.index).iloc[-252:]
        rr = r.iloc[-252:]
        beta = float(np.cov(rr, b.fillna(0))[0, 1] / np.var(b.fillna(0))) if b.notna().sum() > 100 else np.nan

        d = int(dec_today[s])
        hist_med = float(calm.loc[d, "median"])
        tgt = fund["target_mean_price"].get(s) if s in fund.index else None
        analyst_3m = float(np.clip((tgt / last - 1) / 4, -0.15, 0.15)) if tgt and tgt > 0 else None
        exp3 = hist_med if analyst_3m is None else 0.6 * hist_med + 0.4 * analyst_3m
        rows.append({
            "symbol": s, "run_date": run_date, "market": mkt, "price": last, "decile": d,
            "hist_median_3m": hist_med, "hist_p10_3m": float(calm.loc[d, "p10"]),
            "hist_prob_pos": float(calm.loc[d, "prob_pos"]), "analyst_3m": analyst_3m,
            "exp_ret_3m": exp3, "low_3m": exp3 - vol_3m, "high_3m": exp3 + vol_3m,
            "prob_gain": _norm_cdf(exp3 / vol_3m) if vol_3m > 0 else None,
            "vol_3m": vol_3m, "var95_3m": var95, "max_dd_1y": dd, "beta": beta,
            "reward_risk": exp3 / vol_3m if vol_3m > 0 else None,
            "above_200d": bool(pf.loc[s, "above_200d"]),
        })
    df = pd.DataFrame(rows)
    # Pareto frontier on (higher exp_ret, lower vol), among uptrend names
    df["pareto"] = False
    up = df[df["above_200d"]].sort_values("vol_3m")
    best = -np.inf
    for i, r in up.iterrows():
        if r.exp_ret_3m > best:
            df.at[i, "pareto"] = True
            best = r.exp_ret_3m
    df["rr_rank"] = df["reward_risk"].rank(ascending=False, method="first").astype(int)
    db.execute("DELETE FROM market.forecasts WHERE run_date = :d", d=run_date)
    db.upsert("market.forecasts", df, ["symbol", "run_date"])
    return df.sort_values("rr_rank")
